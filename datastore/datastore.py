from abc import ABC, abstractmethod
from typing import Dict, List, Optional
import asyncio
from loguru import logger

from models.models import (
    Document,
    DocumentChunk,
    DocumentMetadataFilter,
    Query,
    QueryResult,
    QueryWithEmbedding,
)
from services.chunks import get_document_chunks
from services.openai import get_embeddings


class DataStore(ABC):
    async def upsert(
        self, documents: List[Document], namespace: Optional[str] = None, chunk_token_size: Optional[int] = None
    ) -> List[str]:
        """
        Takes in a list of documents and inserts them into the database.
        First deletes all the existing vectors with the document id (if necessary, depends on the vector db), then inserts the new ones.
        Return a list of document ids.
        """
        
        # Delete any existing vectors for documents with the input document ids

        # DELETING WITH FILTERS IS NOT SUPPORTED with the Pinecone setup we're using

        # logger.info('UPSERTING');
        # await asyncio.gather(
        #     *[
        #         self.delete(
        #             filter=DocumentMetadataFilter(
        #                 document_id=document.id,
        #             ), 
        #             delete_all=False,
        #             namespace=namespace
        #         )
        #         for document in documents
        #         if document.id
        #     ]
        # )

        chunks_result = get_document_chunks(documents, chunk_token_size)
        chunks = chunks_result["chunks"]
        return await self._upsert(chunks, namespace)

    @abstractmethod
    async def _upsert(self, chunks: Dict[str, List[DocumentChunk]], namespace: str) -> List[str]:
        """
        Takes in a list of list of document chunks and inserts them into the database.
        Return a list of document ids.
        """

        raise NotImplementedError

    async def query(self, queries: List[Query], namespace: str) -> List[QueryResult]:
        """
        Takes in a list of queries and filters and returns a list of query results with matching document chunks and scores.
        """
        
        # get a list of of just the queries from the Query list
        query_texts = [query.query for query in queries]
        embeddings_result = get_embeddings(query_texts)        
        # hydrate the queries with embeddings
        queries_with_embeddings = [
            QueryWithEmbedding(**query.dict(), embedding=embedding)
            for query, embedding in zip(queries, embeddings_result['embeddings'])
        ]
        return {
            "results": await self._query(queries_with_embeddings, namespace),
            "usage": embeddings_result['usage']
        }

    @abstractmethod
    async def _query(self, queries: List[QueryWithEmbedding], namespace: str) -> List[QueryResult]:
        """
        Takes in a list of queries with embeddings and filters and returns a list of query results with matching document chunks and scores.
        """
        raise NotImplementedError
    
    @abstractmethod
    async def stats(self):
        """
        Returns the stats about the embeddings index
        """
        raise NotImplementedError

    @abstractmethod
    async def delete(
        self,
        ids: Optional[List[str]] = None,
        filter: Optional[DocumentMetadataFilter] = None,
        delete_all: Optional[bool] = None,
        namespace: Optional[str] = None
    ) -> bool:
        """
        Removes vectors by ids, filter, or everything in the datastore.
        Multiple parameters can be used at once.
        Returns whether the operation was successful.
        """
        raise NotImplementedError
