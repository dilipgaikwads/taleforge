"""
VideoFactory - research provider (real, checkable sources).

ResearchProvider is the general shape every research source must follow.
WikipediaResearch is the first one: it uses Wikipedia's official public API,
which is free, needs no key, and returns real articles with real links.

The AI is only allowed to use facts found in these articles, and the
fact-checker later confirms each fact against the article text. This is
how the factory avoids invented facts and invented citations.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "VideoFactory/0.1 (personal educational short-video tool; Python urllib)"
MAX_CHARS_PER_ARTICLE = 7000
MIN_CHARS_PER_ARTICLE = 600
END_SECTIONS = ("See also", "References", "Notes", "Further reading", "External links",
                "Sources", "Bibliography", "Citations", "Footnotes")


class ResearchError(Exception):
    """A plain-language problem while collecting sources."""


class ResearchProvider:
    name = "base"

    def collect(self, queries, max_articles=4):
        """Return a list of {'title', 'url', 'text'} sources."""
        raise NotImplementedError


class WikipediaResearch(ResearchProvider):
    name = "wikipedia"

    def __init__(self, language="en", timeout=30):
        self.api = f"https://{language}.wikipedia.org/w/api.php"
        self.language = language
        self.timeout = timeout

    def _get(self, params):
        query = urllib.parse.urlencode({**params, "format": "json", "formatversion": "2"})
        request = urllib.request.Request(f"{self.api}?{query}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ResearchError(f"Wikipedia answered with error {exc.code}. It may be busy; will retry.")
        except (urllib.error.URLError, TimeoutError):
            raise ResearchError("Could not reach Wikipedia. Check the Mac's internet connection.")

    def search(self, query, limit=2):
        data = self._get({"action": "query", "list": "search", "srsearch": query,
                          "srlimit": limit, "srnamespace": 0})
        return [hit["title"] for hit in data.get("query", {}).get("search", [])]

    @staticmethod
    def _clean(text):
        # Cut off reference sections and tidy whitespace.
        for heading in END_SECTIONS:
            match = re.search(rf"\n==+\s*{re.escape(heading)}\s*==+", text)
            if match:
                text = text[:match.start()]
        text = re.sub(r"\n==+\s*(.*?)\s*==+\n", r"\n[\1]\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def article(self, title):
        data = self._get({"action": "query", "prop": "extracts|info", "explaintext": 1,
                          "inprop": "url", "titles": title, "redirects": 1})
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            return None
        page = pages[0]
        text = self._clean(page.get("extract", "") or "")
        if len(text) < MIN_CHARS_PER_ARTICLE or "may refer to" in text[:300]:
            return None
        url = page.get("fullurl") or (
            f"https://{self.language}.wikipedia.org/wiki/" + urllib.parse.quote(page["title"].replace(" ", "_")))
        return {"title": page["title"], "url": url, "text": text[:MAX_CHARS_PER_ARTICLE]}

    def collect(self, queries, max_articles=4):
        titles = []
        for query in queries:
            query = (query or "").strip()
            if not query:
                continue
            for title in self.search(query):
                if title not in titles:
                    titles.append(title)
            if len(titles) >= max_articles * 2:
                break
        sources = []
        for title in titles:
            found = self.article(title)
            if found and all(found["title"] != s["title"] for s in sources):
                sources.append(found)
            if len(sources) >= max_articles:
                break
        return sources
