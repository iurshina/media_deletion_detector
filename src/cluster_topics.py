#!/usr/bin/env python3
"""
Cluster `deleted` articles by topic.

Pipeline:
  1. Load saved_articles/deleted_*.json (the per-finding JSONs written by
     deletion_detector.py). Use `archive.text` only.
  2. Translate each Russian article body to English with Argos, caching by
     SHA-1 of the source so re-runs are cheap. Both the original Russian and
     the English translation are kept in the output (the "mapping").
  3. Embed the Russian text with BGE-M3 via sentence-transformers (BGE-M3 is
     multilingual; embedding the source avoids translation loss).
  4. Fit BERTopic on the English translations using those embeddings, so the
     topic keywords come out as readable English.
  5. Write clusters.json (full data + per-cluster keywords) and clusters.csv
     (one row per article, sorted by cluster) into --output-dir.

Heavy deps are lazy-imported in main() so `--help` stays fast.

Install once:
    uv sync --extra cluster

Then:
    uv run src/cluster_topics.py --input-dir saved_articles --output-dir clusters_output
"""

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import sys


def markdown_to_plaintext(md):
    """Strip the Markdown decorations trafilatura inserts so the text we hand
    to Argos / BERTopic is plain prose."""
    if not md:
        return ""
    text = md
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)            # headings
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)               # [text](url) -> text
    text = re.sub(r"`([^`]+)`", r"\1", text)                          # `code`
    text = re.sub(r"[*_]{1,2}([^*_]+)[*_]{1,2}", r"\1", text)         # *em* / **strong**
    text = re.sub(r"\s+", " ", text).strip()
    return text


def load_deleted_findings(input_dir):
    """Yield one dict per `deleted_*.json` with extractable archive text."""
    paths = sorted(glob.glob(os.path.join(input_dir, "deleted_*.json")))
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                obj = json.load(f)
        except Exception as e:
            print(f"  [!] skipping {p}: {e}", file=sys.stderr)
            continue
        if obj.get("verdict") != "deleted":
            continue
        archive = obj.get("archive") or {}
        text_md = archive.get("text")
        if not text_md:
            continue
        yield {
            "source_file": p,
            "url": obj.get("url"),
            "archive_url": archive.get("url"),
            "archive_timestamp": archive.get("timestamp"),
            "text_ru": markdown_to_plaintext(text_md),
        }


def ensure_argos_translation(from_code, to_code):
    """Return a `Translation` object for from_code -> to_code, installing the
    Argos language package on first run."""
    import argostranslate.package
    import argostranslate.translate

    def _find_translation():
        langs = argostranslate.translate.get_installed_languages()
        src = next((l for l in langs if l.code == from_code), None)
        dst = next((l for l in langs if l.code == to_code), None)
        if src and dst:
            tr = src.get_translation(dst)
            if tr:
                return tr
        return None

    tr = _find_translation()
    if tr is not None:
        return tr

    print(f"  [+] downloading Argos {from_code}->{to_code} language pack...")
    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()
    pkg = next((p for p in available if p.from_code == from_code and p.to_code == to_code), None)
    if pkg is None:
        raise RuntimeError(f"No Argos package available for {from_code}->{to_code}")
    download_path = pkg.download()
    argostranslate.package.install_from_path(download_path)

    tr = _find_translation()
    if tr is None:
        raise RuntimeError(f"Argos {from_code}->{to_code} install reported success but the translation is not available")
    return tr


def translate_with_cache(texts, translation, cache_file):
    """Translate `texts` with `translation`, caching results by SHA-1 of the
    source string. The cache is persisted after every 10 new entries so an
    interrupted run keeps its progress."""
    cache = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            cache = {}

    out = []
    new = 0
    for i, text in enumerate(texts, 1):
        key = hashlib.sha1(text.encode("utf-8")).hexdigest()
        if key in cache:
            out.append(cache[key])
            continue
        try:
            en = translation.translate(text)
        except Exception as e:
            print(f"  [!] translation failed for item {i}: {e}", file=sys.stderr)
            en = ""
        cache[key] = en
        out.append(en)
        new += 1
        if new % 10 == 0:
            print(f"  [+] translated {new} new / {len(texts)} total...")
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Cluster deleted articles by topic (BGE-M3 + BERTopic, Argos RU->EN)."
    )
    parser.add_argument("--input-dir", default="saved_articles",
                        help="Directory containing deleted_*.json findings")
    parser.add_argument("--output-dir", default="clusters_output",
                        help="Where to write clusters.json, clusters.csv, and the translation cache")
    parser.add_argument("--model", default="BAAI/bge-m3",
                        help="sentence-transformers model id (default BAAI/bge-m3)")
    parser.add_argument("--min-topic-size", type=int, default=10,
                        help="Minimum cluster size for BERTopic / HDBSCAN")
    parser.add_argument("--max-seq-length", type=int, default=512,
                        help="Max tokens per article for embedding (default 512). BGE-M3 supports up to "
                             "8192, but the attention matrix at 8192 needs ~63 GB even at batch=32 — keep "
                             "this low on CPU. Raise only if you have a GPU with lots of VRAM.")
    parser.add_argument("--batch-size", type=int, default=8,
                        help="Embedding batch size (default 8). Lower if you still OOM, raise on GPU.")
    parser.add_argument("--from-lang", default="ru")
    parser.add_argument("--to-lang", default="en")
    parser.add_argument("--no-translate", action="store_true",
                        help="Skip translation; use Russian text for both embedding and topic labels")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # --- Phase 1: load ---
    print(f"\n[Phase 1] Loading deleted articles from {args.input_dir}/")
    items = list(load_deleted_findings(args.input_dir))
    if not items:
        print("  [!] No deleted_*.json files with extractable archive text were found.", file=sys.stderr)
        sys.exit(1)
    print(f"  [+] Loaded {len(items)} deleted articles.")

    ru_texts = [it["text_ru"] for it in items]

    # --- Phase 2: translate ---
    if not args.no_translate:
        print(f"\n[Phase 2] Translating {args.from_lang} -> {args.to_lang} with Argos")
        translation = ensure_argos_translation(args.from_lang, args.to_lang)
        cache_file = os.path.join(args.output_dir, "translation_cache.json")
        en_texts = translate_with_cache(ru_texts, translation, cache_file)
        for it, en in zip(items, en_texts):
            it["text_en"] = en
    else:
        en_texts = list(ru_texts)
        for it in items:
            it["text_en"] = ""

    # --- Phase 3: embed ---
    print(f"\n[Phase 3] Embedding {len(items)} articles with {args.model} "
          f"(max_seq_length={args.max_seq_length}, batch_size={args.batch_size})")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(args.model)
    # BGE-M3 defaults to 8192 — the attention matrix at that length will OOM
    # any reasonable machine. Cap it.
    model.max_seq_length = args.max_seq_length
    embeddings = model.encode(
        ru_texts,
        batch_size=args.batch_size,
        show_progress_bar=True,
        convert_to_numpy=True,
    )

    # --- Phase 4: cluster ---
    print(f"\n[Phase 4] Clustering with BERTopic (min_topic_size={args.min_topic_size})")
    from bertopic import BERTopic
    from bertopic.representation import MaximalMarginalRelevance
    import numpy as np

    docs_for_labels = en_texts if not args.no_translate else ru_texts
    # MMR picks diverse keywords instead of N near-duplicates ("protest /
    # protests / protester") — cleaner labels for a glance.
    representation_model = MaximalMarginalRelevance(diversity=0.3)
    topic_model = BERTopic(
        embedding_model=model,           # only used if BERTopic needs to re-embed; we pass embeddings directly
        representation_model=representation_model,
        min_topic_size=args.min_topic_size,
        calculate_probabilities=False,
        verbose=True,
    )
    topics, _ = topic_model.fit_transform(docs_for_labels, embeddings)

    for i, it in enumerate(items):
        it["cluster"] = int(topics[i])

    # --- Phase 5: representative articles per cluster (closest to centroid) ---
    def _representatives_by_centroid(embs, topic_ids, top_n=3):
        reps = {}
        topic_arr = np.array(topic_ids)
        for tid in set(topic_ids):
            if tid == -1:
                continue
            indices = np.where(topic_arr == tid)[0]
            cluster_embs = embs[indices]
            centroid = cluster_embs.mean(axis=0)
            # cosine similarity to centroid
            norms = np.linalg.norm(cluster_embs, axis=1) * np.linalg.norm(centroid)
            sims = (cluster_embs @ centroid) / np.where(norms == 0, 1e-12, norms)
            order = np.argsort(-sims)[:top_n]
            reps[tid] = indices[order].tolist()
        return reps

    representatives = _representatives_by_centroid(embeddings, topics, top_n=3)

    def _snippet(text, n=300):
        if not text:
            return ""
        s = text[:n]
        if len(text) > n:
            s = s.rsplit(" ", 1)[0] + "…"
        return s

    # --- Phase 6: assemble output ---
    cluster_rows = []
    info = topic_model.get_topic_info()
    for _, row in info.iterrows():
        tid = int(row["Topic"])
        keywords = [w for w, _ in (topic_model.get_topic(tid) or [])] if tid != -1 else []
        rep_idxs = representatives.get(tid, [])
        rep_articles = [
            {
                "url": items[i]["url"],
                "archive_url": items[i]["archive_url"],
                "snippet_en": _snippet(items[i].get("text_en") or "", 300),
                "snippet_ru": _snippet(items[i]["text_ru"], 300),
            }
            for i in rep_idxs
        ]
        # Human-friendly one-liner: diverse keywords + a glimpse of the central article.
        desc_parts = []
        if keywords:
            desc_parts.append("Keywords: " + ", ".join(keywords[:6]))
        if rep_articles and rep_articles[0]["snippet_en"]:
            desc_parts.append("Example: " + _snippet(rep_articles[0]["snippet_en"], 180))
        description = " | ".join(desc_parts) if desc_parts else "(outliers — no shared topic)"

        cluster_rows.append({
            "id": tid,
            "size": int(row["Count"]),
            "label": str(row.get("Name", "")),
            "description": description,
            "keywords_en": keywords[:10],
            "representative_articles": rep_articles,
        })

    output = {
        "params": {
            "input_dir": args.input_dir,
            "model": args.model,
            "min_topic_size": args.min_topic_size,
            "translation": f"{args.from_lang}->{args.to_lang}" if not args.no_translate else None,
        },
        "n_articles": len(items),
        "n_topics": sum(1 for c in cluster_rows if c["id"] != -1),
        "n_outliers": sum(1 for t in topics if t == -1),
        "articles": [
            {
                "source_file": it["source_file"],
                "url": it["url"],
                "archive_url": it["archive_url"],
                "archive_timestamp": it["archive_timestamp"],
                "cluster": it["cluster"],
                "text_ru": it["text_ru"],
                "text_en": it["text_en"],
            }
            for it in items
        ],
        "clusters": cluster_rows,
    }

    out_json = os.path.join(args.output_dir, "clusters.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n  [+] Wrote {out_json}")

    # Human-readable summary, scannable in a terminal.
    out_txt = os.path.join(args.output_dir, "summary.txt")
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(f"Clusters from {output['n_articles']} deleted articles "
                f"({output['n_topics']} topics, {output['n_outliers']} outliers)\n")
        f.write("=" * 78 + "\n\n")
        ordered = sorted(cluster_rows, key=lambda c: (c["id"] == -1, -c["size"]))
        for c in ordered:
            tag = "OUTLIERS" if c["id"] == -1 else f"Cluster {c['id']}"
            f.write(f"{tag}  ({c['size']} articles)\n")
            if c["keywords_en"]:
                f.write("  Keywords: " + ", ".join(c["keywords_en"][:8]) + "\n")
            for r in c["representative_articles"]:
                snippet = r["snippet_en"] or r["snippet_ru"]
                f.write(f"  - {r['url']}\n")
                if snippet:
                    f.write(f"      {snippet}\n")
            f.write("\n")
    print(f"  [+] Wrote {out_txt}")

    out_csv = os.path.join(args.output_dir, "clusters.csv")
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["cluster", "url", "archive_timestamp", "source_file", "text_en_preview"])
        for it in sorted(items, key=lambda x: (x["cluster"], x["archive_timestamp"] or "")):
            preview = (it.get("text_en") or it["text_ru"])[:200].replace("\n", " ")
            writer.writerow([it["cluster"], it["url"], it["archive_timestamp"], it["source_file"], preview])
    print(f"  [+] Wrote {out_csv}")

    print(
        f"\n✅ Done. {output['n_topics']} topics, {output['n_outliers']} outliers "
        f"out of {output['n_articles']} articles."
    )


if __name__ == "__main__":
    main()
