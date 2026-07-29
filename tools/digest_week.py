#!/usr/bin/env python3
"""Condense a reflowed rep-week into the evidence a SILO rollup actually cites.

Per call: the opening (Ready/Welcome), the tech turns that hit SILO scripted
markers (Assessment teaching, Options A/B/C, Option C handoff, objections,
review ask), and the ending (Deliver). Rapport-only turns are dropped.
"""
import re, sys

MARK = re.compile(
    r"equipment specialist|comfort advisor|option c|system update|updating the comfort|"
    r"thorough evaluation|five star|5 star|5-star|google review|review|"
    r"immediate repair|upcoming repair|option a|option b|approval|approve|autograph|signature|"
    r"not my department|fix-it guy|fix it guy|membership|"
    r"amp|volt|degree|rated|reading|capacitor|compressor|coil|refrigerant|leak|motor|"
    r"what questions|how long have you|what are you hoping|parked|come on in|pleasure of speaking",
    re.I,
)


def main():
    src = sys.argv[1]
    t = open(src, encoding="utf-8").read()
    parts = re.split(r"\n## (CALL \d+ — .*)\n", t)
    out = [parts[0].strip()]
    for i in range(1, len(parts), 2):
        hdr, body = parts[i], parts[i + 1]
        turns = [x.strip() for x in body.strip().split("\n\n") if x.strip()]
        tech = [x for x in turns if not x.startswith("**Customer")]
        keep, seen = [], set()
        for j, x in enumerate(turns[:14]):          # opening
            keep.append((j, x))
        for j, x in enumerate(turns):
            if x in seen:
                continue
            if not x.startswith("**Customer") and MARK.search(x) and len(x) > 160:
                keep.append((j, x))
                seen.add(x)
                nxt = turns[j + 1] if j + 1 < len(turns) else ""
                if nxt.startswith("**Customer") and len(nxt) > 40:
                    keep.append((j + 0.5, nxt))
        for j, x in enumerate(turns[-10:]):          # ending
            keep.append((len(turns) - 10 + j, x))
        keep = sorted({(k, v) for k, v in keep})
        out.append("\n\n### %s\n" % hdr)
        last = -99
        for k, v in keep:
            if k - last > 1.6:
                out.append("   …")
            out.append(v[:1400])
            last = k
    dest = src.replace(".md", "_digest.md")
    open(dest, "w", encoding="utf-8").write("\n\n".join(out))
    print("wrote", dest, len(open(dest,encoding='utf-8').read().split()), "words")


if __name__ == "__main__":
    main()
