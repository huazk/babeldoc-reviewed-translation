"""Render review sheets, inspect image text, build and verify alternating PDFs."""
import argparse
import json
from pathlib import Path
import re


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def reject_overwrite(output, *inputs):
    if output.resolve() in [p.resolve() for p in inputs if p]:
        raise ValueError("Output must differ from every input")


def qa(args, fitz):
    from PIL import Image, ImageDraw, ImageOps
    args.out.mkdir(parents=True, exist_ok=True)
    stats = []
    with fitz.open(args.input) as doc:
        for i, page in enumerate(doc):
            text = page.get_text()
            stats.append({"page": i + 1, "chinese_chars": len(re.findall(r"[\u4e00-\u9fff]", text)), "text": text})
            page.get_pixmap(matrix=fitz.Matrix(args.scale, args.scale), alpha=False).save(str(args.out / f"page-{i+1:03}.png"))
        for start in range(0, len(doc), 9):
            canvas = Image.new("RGB", (1440, 1140), "#e5e7eb")
            draw = ImageDraw.Draw(canvas)
            for j, index in enumerate(range(start, min(start + 9, len(doc)))):
                with Image.open(args.out / f"page-{index+1:03}.png") as im:
                    thumb = ImageOps.contain(im.convert("RGB"), (472, 350))
                    x, y = j % 3 * 480, j // 3 * 380
                    canvas.paste(thumb, (x + (480-thumb.width)//2, y + 24))
                    draw.text((x+5, y+4), str(index+1), fill="red")
            canvas.save(args.out / f"contact-{start//9+1:03}.png")
    save_json(args.out / "stats.json", stats)
    print("RENDERED", len(stats), "LOW_CHINESE", [s["page"] for s in stats if s["chinese_chars"] < 10])


def ocr(args, fitz):
    from rapidocr_onnxruntime import RapidOCR
    engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=2)
    items, skipped = [], []
    with fitz.open(args.input) as doc:
        for pn, page in enumerate(doc):
            if page.rotation:
                skipped.append({"page": pn+1, "reason": "rotation requires explicit coordinate transform"})
                continue
            spans = [s for b in page.get_text("dict")["blocks"] if "lines" in b for line in b["lines"] for s in line["spans"]]
            rects, seen = [], set()
            for info in page.get_image_info():
                rect = fitz.Rect(info["bbox"]) & page.rect
                key = tuple(round(x, 2) for x in rect)
                if rect.width >= 60 and rect.height >= 25 and key not in seen:
                    seen.add(key)
                    rects.append(rect)
            for rect in rects:
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
                result, _ = engine(pix.tobytes("png"))
                for box, text, score in result or []:
                    if not re.search(r"[A-Za-z]{2}", text):
                        continue
                    xs, ys = [p[0] for p in box], [p[1] for p in box]
                    # Pixmap origins account for clip rounding at image edges.
                    target = fitz.Rect((min(xs)+pix.x)/2, (min(ys)+pix.y)/2, (max(xs)+pix.x)/2, (max(ys)+pix.y)/2)
                    overlap = any((target & fitz.Rect(s["bbox"])).get_area() > target.get_area() * .28 for s in spans)
                    items.append({"page": pn+1, "bbox": list(target), "text": text, "score": float(score), "native_overlap": overlap})
            print("OCR_PAGE", pn+1, "CANDIDATES", len(items), flush=True)
    save_json(args.out, {"items": items, "skipped_pages": skipped})


def dual(args, fitz):
    reject_overwrite(args.out, args.input, args.translated)
    with fitz.open(args.input) as source, fitz.open(args.translated) as translated, fitz.open() as result:
        if len(source) != len(translated):
            raise ValueError("Page counts differ; supply matching original/translated page subsets")
        for i in range(len(source)):
            if source[i].rect != translated[i].rect:
                raise ValueError(f"Page dimensions differ at page {i+1}")
            result.insert_pdf(source, from_page=i, to_page=i)
            result.insert_pdf(translated, from_page=i, to_page=i)
        result.set_metadata({"title": args.input.stem + " - bilingual", "creator": "BabelDOC reviewed translation"})
        args.out.parent.mkdir(parents=True, exist_ok=True)
        result.subset_fonts()
        result.save(args.out, garbage=4, deflate=True)
        print("DUAL_PAGES", len(result))


def verify(args, fitz):
    with fitz.open(args.input) as source, fitz.open(args.translated) as translated, fitz.open(args.dual) as bilingual:
        if not len(source) or len(source) != len(translated) or len(bilingual) != 2*len(source):
            raise ValueError("Incorrect page count")
        for i in range(len(source)):
            if not source[i].rect == translated[i].rect == bilingual[2*i].rect == bilingual[2*i+1].rect:
                raise ValueError(f"Dimensions differ at page {i+1}")
        indices = range(len(source)) if args.all_pages else sorted({0, len(source)//2, len(source)-1})
        for i in indices:
            for original, copied in [(source[i], bilingual[2*i]), (translated[i], bilingual[2*i+1])]:
                a, b = original.get_pixmap(alpha=False), copied.get_pixmap(alpha=False)
                if (a.width, a.height, a.samples) != (b.width, b.height, b.samples):
                    raise ValueError(f"Pixel mismatch at source/translation pair {i+1}")
        print("VERIFIED", len(source), "PAIRS_CHECKED", len(indices))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["qa", "ocr", "dual", "verify"])
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--translated", type=Path)
    parser.add_argument("--dual", type=Path)
    parser.add_argument("--all-pages", action="store_true")
    parser.add_argument("--scale", type=float, default=1.5)
    args = parser.parse_args()
    if args.mode != "verify" and not args.out:
        parser.error("--out is required")
    if args.mode in ("dual", "verify") and not args.translated:
        parser.error("--translated is required")
    if args.mode == "verify" and not args.dual:
        parser.error("--dual is required")
    if args.scale <= 0:
        parser.error("--scale must be positive")
    if args.out:
        reject_overwrite(args.out, args.input, args.translated)
    import fitz
    {"qa": qa, "ocr": ocr, "dual": dual, "verify": verify}[args.mode](args, fitz)


if __name__ == "__main__":
    main()
