#!/usr/bin/env python3
"""Reflow a rep-week of transcripts into one compact digest + a truth manifest.

The raw transcripts are stored one word per line, which inflates them ~5x.
This joins each speaker turn back into a paragraph and writes:
  <out>/<Rep_Name>_<end>.md   -- numbered calls, reflowed
  prints a manifest: call #, date, job, words, TGL-flip status
"""
import json, glob, os, re, sys

REPO = "/home/user/sierra-ropp-hourly"


def truth_map():
    tg = {}
    for f in sorted(glob.glob(os.path.join(REPO, "tgl_truth", "20*.json"))):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for job, v in d.get("tgls", {}).items():
            tg.setdefault(job, (os.path.basename(f)[:-5], v.get("lead")))
    return tg


def reflow(path):
    raw = open(path, encoding="utf-8", errors="replace").read()
    head, _, body = raw.partition("=" * 60)
    turns = []
    speaker, buf = None, []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.endswith(":") and len(s) < 40 and not s.startswith(("-", "•")):
            if speaker and buf:
                turns.append((speaker, " ".join(buf)))
            speaker, buf = s[:-1], []
            continue
        buf.append(s)
    if speaker and buf:
        turns.append((speaker, " ".join(buf)))
    out = []
    for sp, txt in turns:
        txt = re.sub(r"\s+([,.?!])", r"\1", txt)
        txt = re.sub(r"\s{2,}", " ", txt).strip()
        if txt:
            out.append("**%s:** %s" % (sp, txt))
    return head.strip(), "\n\n".join(out)


def main():
    rep, start, end, outdir = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    tg = truth_map()
    days = sorted(
        d for d in os.listdir(os.path.join(REPO, "transcripts"))
        if start <= d <= end
    )
    files = []
    for day in days:
        p = os.path.join(REPO, "transcripts", day)
        for fn in sorted(os.listdir(p)):
            if fn.startswith(rep + " -"):
                files.append((day, os.path.join(p, fn), fn))
    os.makedirs(outdir, exist_ok=True)
    chunks, manifest = [], []
    for i, (day, path, fn) in enumerate(files, 1):
        m = re.search(r"Job # (\d+)", fn)
        job = m.group(1) if m else ""
        head, body = reflow(path)
        words = len(body.split())
        flip = "FLIP" if job and job in tg else "-"
        cust = fn.split(" - ", 1)[1].rsplit(", Job #", 1)[0][:44]
        manifest.append(
            {"call": i, "date": day, "job": job, "customer": cust, "words": words, "tgl": flip}
        )
        chunks.append(
            "\n\n## CALL %d — %s — %s — Job %s — %s — %d words\n\n%s"
            % (i, day, cust, job or "(no job #)", flip, words, body)
        )
    name = rep.replace("  ", " ").replace(" ", "_")
    dest = os.path.join(outdir, "%s.md" % name)
    open(dest, "w", encoding="utf-8").write(
        "# %s — %s to %s\n" % (rep, start, end) + "".join(chunks)
    )
    print(json.dumps(manifest, indent=0))
    print("TOTAL calls=%d words=%d -> %s" % (len(files), sum(m["words"] for m in manifest), dest))


if __name__ == "__main__":
    main()
