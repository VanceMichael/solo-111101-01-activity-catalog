from html.parser import HTMLParser


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.items = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "article" and "data-id" in values:
            self.items.append({key[5:]: value for key, value in values.items() if key.startswith("data-")})
        if tag == "a" and values.get("rel") == "next":
            self.items.append({"kind": "pagination", "next": values["href"]})


def decode(content):
    parser = PageParser()
    parser.feed(content.decode("utf-8"))
    return parser.items
