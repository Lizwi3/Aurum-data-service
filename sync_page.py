"""Copy ../standalone/aurum.html to index.html with the live data service switched on.
Run after every change to the page:  python sync_page.py"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "standalone", "aurum.html")
# The page calls the function file directly (/api/agsi_latest); /api/agsi-latest also works via vercel.json.
META = '<meta name="aurum-api" content="/api/agsi_latest">'


def build(src_text):
    if src_text.lstrip().lower().startswith("<!doctype"):
        raise ValueError("source page already has a document wrapper")
    head = "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n" \
           "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">\n" + META + "\n"
    i = src_text.index("</style>") + len("</style>")
    # title, meta, fonts, chart script and styles go in <head>; the rest is the body
    return head + src_text[:i] + "\n</head>\n<body>\n" + src_text[i:] + "\n</body>\n</html>\n"


if __name__ == "__main__":
    with open(SRC, encoding="utf-8") as f:
        out = build(f.read())
    with open(os.path.join(HERE, "index.html"), "w", encoding="utf-8") as f:
        f.write(out)
    print("index.html updated")
