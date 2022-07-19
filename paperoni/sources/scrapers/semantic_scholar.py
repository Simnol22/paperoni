import json
import urllib.parse

from coleo import Option, tooled

from ...config import scrapers
from ...utils import QueryError
from ..acquire import HTTPSAcquirer
from ..model import (
    Author,
    DatePrecision,
    Link,
    Paper,
    Release,
    Topic,
    Venue,
    VenueType,
)

external_ids_mapping = {
    "pubmedcentral": "pmc",
}


venue_type_mapping = {
    "JournalArticle": VenueType.journal,
    "Conference": VenueType.conference,
    "Book": VenueType.book,
    "Review": VenueType.review,
    "_": VenueType.unknown,
}


def _paper_long_fields(parent=None, extras=()):
    fields = (
        "paperId",
        "externalIds",
        "url",
        "title",
        "abstract",
        "venue",
        "publicationTypes",
        "publicationDate",
        "year",
        "journal",
        "referenceCount",
        "citationCount",
        "influentialCitationCount",
        "isOpenAccess",
        "fieldsOfStudy",
        *extras,
    )
    return (
        fields
        if parent is None
        else tuple(f"{parent}.{field}" for field in fields)
    )


def _paper_short_fields(parent=None):
    fields = (
        "paperId",
        "url",
        "title",
        "venue",
        "year",
        "authors",  # {authorId, name}
    )
    return (
        fields
        if parent is None
        else tuple(f"{parent}.{field}" for field in fields)
    )


def _author_fields(parent=None):
    fields = (
        "authorId",
        "externalIds",
        "url",
        "name",
        "aliases",
        "affiliations",
        "homepage",
        "paperCount",
        "citationCount",
    )
    return (
        fields
        if parent is None
        else tuple(f"{parent}.{field}" for field in fields)
    )


class SemanticScholarQueryManager:
    # "authors" will have fields "authorId" and "name"
    SEARCH_FIELDS = _paper_long_fields(extras=("authors",))
    PAPER_FIELDS = (
        *_paper_long_fields(),
        *_author_fields(parent="authors"),
        *_paper_short_fields(parent="citations"),
        *_paper_short_fields(parent="references"),
        "embedding",
    )
    PAPER_AUTHORS_FIELDS = _author_fields() + _paper_long_fields(
        parent="papers", extras=("authors",)
    )
    PAPER_CITATIONS_FIELDS = (
        "contexts",
        "intents",
        "isInfluential",
        *SEARCH_FIELDS,
    )
    PAPER_REFERENCES_FIELDS = PAPER_CITATIONS_FIELDS
    AUTHOR_FIELDS = _author_fields()  # PAPER_AUTHORS_FIELDS
    AUTHOR_PAPERS_FIELDS = (
        SEARCH_FIELDS
        + _paper_short_fields(parent="citations")
        + _paper_short_fields(parent="references")
    )

    def __init__(self):
        self.conn = HTTPSAcquirer(
            "api.semanticscholar.org",
            delay=5 * 60 / 100,  # 100 requests per 5 minutes
        )

    def _evaluate(self, path: str, **params):
        params = urllib.parse.urlencode(params)
        data = self.conn.get(f"/graph/v1/{path}?{params}")
        jdata = json.loads(data)
        if "error" in jdata:
            raise QueryError(jdata["error"])
        return jdata

    def _list(
        self,
        path: str,
        fields: tuple[str],
        block_size: int = 100,
        limit: int = 10000,
        **params,
    ):
        params = {
            "fields": ",".join(fields),
            "limit": min(block_size or 10000, limit),
            **params,
        }
        next_offset = 0
        while next_offset is not None and next_offset < limit:
            results = self._evaluate(path, offset=next_offset, **params)
            next_offset = results.get("next", None)
            for entry in results["data"]:
                yield entry

    def _wrap_author(self, data):
        lnk = (aid := data["authorId"]) and Link(
            type="semantic_scholar", link=aid
        )
        return Author(
            name=data["name"],
            affiliations=[],
            aliases=data.get("aliases", None) or [],
            links=[lnk] if lnk else [],
            roles=[],
        )

    def _wrap_paper(self, data):
        from hrepr import pstr

        print(pstr(data))
        links = [Link(type="semantic_scholar", link=data["paperId"])]
        for typ, ref in data["externalIds"].items():
            links.append(
                Link(
                    type=external_ids_mapping.get(t := typ.lower(), t), link=ref
                )
            )
        authors = list(map(self._wrap_author, data["authors"]))
        # date = data["publicationDate"] or f'{data["year"]}-01-01'
        if pubd := data["publicationDate"]:
            date = {
                "date": f"{pubd} 00:00",
                "date_precision": DatePrecision.day,
            }
        else:
            date = DatePrecision.assimilate_date(data["year"])
        release = Release(
            venue=Venue(
                type=venue_type_mapping[
                    (pubt := data.get("publicationTypes", []))
                    and pubt[0]
                    or "_"
                ],
                name=data["venue"],
                volume=(j := data["journal"]) and j.get("volume", None),
                links=[],
            ),
            **date,
        )
        return Paper(
            links=links,
            authors=authors,
            title=data["title"],
            abstract=data["abstract"] or "",
            citation_count=data["citationCount"],
            topics=[
                Topic(name=field) for field in (data["fieldsOfStudy"] or ())
            ],
            releases=[release],
            scrapers=["ssch"],
        )

    def search(self, query, fields=SEARCH_FIELDS, **params):
        papers = self._list(
            "paper/search",
            query=query,
            fields=fields,
            **params,
        )
        yield from map(self._wrap_paper, papers)

    def paper(self, paper_id, fields=PAPER_FIELDS):
        (paper,) = self._list(f"paper/{paper_id}", fields=fields)
        return paper

    def paper_authors(self, paper_id, fields=PAPER_AUTHORS_FIELDS, **params):
        yield from self._list(
            f"paper/{paper_id}/authors", fields=fields, **params
        )

    def paper_citations(
        self, paper_id, fields=PAPER_CITATIONS_FIELDS, **params
    ):
        yield from self._list(
            f"paper/{paper_id}/citations", fields=fields, **params
        )

    def paper_references(
        self, paper_id, fields=PAPER_REFERENCES_FIELDS, **params
    ):
        yield from self._list(
            f"paper/{paper_id}/citations", fields=fields, **params
        )

    # def author(self, author_id, fields=AUTHOR_FIELDS, **params):
    #     yield from self._list(f"author/{author_id}", fields=fields, **params)

    def author(self, name, fields=AUTHOR_FIELDS, **params):
        authors = self._list(
            f"author/search", query=name, fields=fields, **params
        )
        yield from map(self._wrap_author, authors)

    def author_papers(self, author_id, fields=AUTHOR_PAPERS_FIELDS, **params):
        papers = self._list(
            f"author/{author_id}/papers", fields=fields, **params
        )
        yield from map(self._wrap_paper, papers)


@tooled
def query(
    # Author to query
    # [alias: -a]
    # [nargs: +]
    author: Option = [],
    # Title of the paper
    # [alias: -t]
    # [nargs: +]
    title: Option = [],
    # Maximal number of results per query
    block_size: Option & int = 100,
    # Maximal number of results to return
    limit: Option & int = 10000,
):
    author = " ".join(author)
    title = " ".join(title)

    if author and title:
        raise QueryError("Cannot query both author and title")

    ss = SemanticScholarQueryManager()

    if title:
        yield from ss.search(title, block_size=block_size, limit=limit)

    elif author:
        for auth in ss.author(author):
            print(auth)


scrapers["semantic_scholar"] = query