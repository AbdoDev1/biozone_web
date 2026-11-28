"""مُثري الشيت — نسخة مستقلة تعمل داخل مساحة عمل الوكيل (stdlib فقط).

المدخلات: sheet.csv (شيت خام) و catalog.csv (تصدير الكتالوج)
المخرجات: ready.csv بالأعمدة القياسية + report.txt
"""
import csv, re, sys
from collections import Counter, defaultdict

TR = str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ة":"ه","ى":"ي","ؤ":"و","ئ":"ي"})

def norm(s):
    s = str(s or "").strip()
    s = re.sub(r"[\u064B-\u0652\u0640]", "", s).translate(TR)
    s = re.sub(r"(\d)([^\d\s])", r"\1 \2", s)
    s = re.sub(r"([^\d\s])(\d)", r"\1 \2", s)
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()

def toks(s):
    return {t for t in norm(s).split() if len(t) >= 2}

ALIASES = {
    "name": ["الصنف","الاسم","اسم الصنف","البيان","item","name"],
    "price": ["سعر ج","السعر","سعر","سعر الجمهور","price"],
    "qty": ["الكمية","كمية","qty"],
    "barcode": ["الباركود","باركود","barcode"],
    "company": ["الشركة","الشركه","البراند","الماركة","brand","company"],
    "group": ["القسم","قسم","المجموعة","group"],
    "uom": ["الوحدة","وحده","uom"],
}
HDR = {norm(a): k for k, v in ALIASES.items() for a in v}

def read_csv(path):
    for enc in ("utf-8-sig","cp1256","windows-1252"):
        try:
            text = open(path, encoding=enc).read(); break
        except UnicodeDecodeError:
            continue
    rows = list(csv.reader(text.splitlines()))
    return [[c.strip() for c in r] for r in rows if any(r)]

def load_catalog(path):
    rows = read_csv(path)
    head = [h.strip() for h in rows[0]]
    idx = {h: i for i, h in enumerate(head)}
    items = []
    for r in rows[1:]:
        code = r[idx["item_code"]] if "item_code" in idx else ""
        name = r[idx["item_name"]] if "item_name" in idx else ""
        if not code:
            continue
        items.append({
            "code": code, "name": name,
            "group": r[idx["item_group"]] if "item_group" in idx else "",
            "brand": r[idx["brand"]] if "brand" in idx else "",
            "uom": r[idx["stock_uom"]] if "stock_uom" in idx else "",
        })
    return items

def main():
    raw = read_csv("sheet.csv")
    items = load_catalog("catalog.csv")
    by_name = {norm(i["name"]): i for i in items}
    toks_by = {i["code"]: toks(i["name"]) for i in items}
    brand_votes = defaultdict(Counter)
    for i in items:
        if i["brand"]:
            for t in toks_by[i["code"]]:
                brand_votes[t][i["brand"]] += 1

    def neighbors(name, k=5):
        ts = toks(name); out = []
        for i in items:
            o = toks_by[i["code"]]
            if not o: continue
            out.append((len(ts & o) / (len(ts | o) or 1), i))
        out.sort(key=lambda x: x[0], reverse=True)
        return [(s, i) for s, i in out[:k] if s > 0]

    def brand_for(company):
        v = Counter()
        for t in toks(company):
            if t in brand_votes: v.update(brand_votes[t])
        return v.most_common(1)[0][0] if v else ""

    # صف الترويسة: أول صف فيه عمود اسم
    hidx = next((i for i, r in enumerate(raw) if any(norm(c) in ("name","price") for c in r)), 0)
    head = raw[hidx]
    col = {}
    for j, h in enumerate(head):
        k = HDR.get(norm(h))
        if k and k not in col: col[k] = j
    if "name" not in col:
        print("لا يوجد عمود اسم في الشيت", file=sys.stderr); sys.exit(2)

    def val(r, key):
        j = col.get(key)
        return r[j] if j is not None and j < len(r) else ""

    next_no = max([int(i["code"][3:]) for i in items if i["code"].startswith("BZ-") and i["code"][3:].isdigit()] or [1]) + 1
    used = {i["code"] for i in items}
    out, report = [], Counter()
    seen = set()

    for n, r in enumerate(raw[hidx+1:], start=hidx+2):
        name = val(r, "name").strip()
        if not name:
            report["اسم فارغ"] += 1; continue
        key = norm(name)
        if key in seen:
            report["مكرر داخل الملف"] += 1; continue
        seen.add(key)
        price = val(r, "price").strip()
        if price:
            try: float(price.replace(",", ""))
            except ValueError:
                report["سعر غير صالح"] += 1; continue
            price = f"{float(price.replace(',', '')):g}"

        hit = by_name.get(key)
        if hit:
            action, code, group, brand, uom = "update", hit["code"], hit["group"], hit["brand"], hit["uom"]
        else:
            action = "create"
            while f"BZ-{next_no}" in used: next_no += 1
            code = f"BZ-{next_no}"; used.add(code); next_no += 1
            near = neighbors(name)
            group = val(r, "group").strip() or (near[0][1]["group"] if near and near[0][0] >= 0.66 else "مستلزمات عامة")
            uom = val(r, "uom").strip() or (near[0][1]["uom"] if near and near[0][0] >= 0.66 else "قطعة")
            brand = val(r, "company").strip()
            brand = brand_for(brand) or (brand if brand else "")
        if not brand: report["بلا براند"] += 1
        report["جديد" if action == "create" else "محدّث"] += 1
        out.append([name, val(r, "barcode"), code, price, val(r, "qty"), "", uom, "", group, brand])

    headers = ["الاسم","الباركود","الكود","سعر الجمهور","الكمية","الوحدة الكبرى","الوحدة","عدد الوحدات في الكبرى","القسم","البراند"]
    with open("ready.csv", "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh); w.writerow(headers); w.writerows(out)
    with open("report.txt", "w", encoding="utf-8") as fh:
        fh.write("\n".join(f"{k}: {v}" for k, v in report.items()))
    print("READY", len(out), dict(report))

if __name__ == "__main__":
    main()
