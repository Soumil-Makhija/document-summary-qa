"""Model-free source search shared by the CLI and HTTP interface."""

from .core import DocumentError


def search_document(store, document_id, query):
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise DocumentError("Enter a search query between 1 and 2,000 characters.")
    query = query.strip()
    results = [{**passage, "url": f"/api/documents/{document_id}/file#page={passage['page']}"}
               for passage in store.search(document_id, query, limit=12)]
    return {"document_id": document_id, "query": query, "results": results}
