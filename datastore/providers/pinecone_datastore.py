import os
from typing import Any, Dict, List, Optional
from pinecone import Pinecone
from tenacity import retry, wait_random_exponential, stop_after_attempt
import asyncio
from loguru import logger

from datastore.datastore import DataStore
from models.models import (
    DocumentChunk,
    DocumentChunkMetadata,
    DocumentChunkWithScore,
    DocumentMetadataFilter,
    QueryResult,
    QueryWithEmbedding,
    Source,
)
from services.date import to_unix_timestamp

# Read environment variables for Pinecone configuration
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
PINECONE_INDEX_HOST = os.environ.get("PINECONE_INDEX_HOST")
assert PINECONE_API_KEY is not None
assert PINECONE_INDEX_HOST is not None

# Initialize Pinecone with the API key and environment
pc = Pinecone(api_key=PINECONE_API_KEY)

# Set the batch size for upserting vectors to Pinecone
UPSERT_BATCH_SIZE = 100

EMBEDDING_DIMENSION = int(os.environ.get("EMBEDDING_DIMENSION", 256))

class PineconeDataStore(DataStore):
    def __init__(self):
        # Connect to an existing index with the specified name
        try:
            logger.info(f"Connecting to existing index {PINECONE_INDEX_HOST}")
            self.index = pc.Index(host=PINECONE_INDEX_HOST)
            logger.info(f"Connected to index {PINECONE_INDEX_HOST} successfully")
        except Exception as e:
            logger.error(f"Error connecting to index {PINECONE_INDEX_HOST}: {e}")
            raise e

    @retry(wait=wait_random_exponential(min=1, max=20), stop=stop_after_attempt(3))
    async def stats(self):
        index_stats_response = self.index.describe_index_stats()
        logger.info(index_stats_response)
        return index_stats_response

    @retry(wait=wait_random_exponential(min=1, max=20), stop=stop_after_attempt(3))
    async def _upsert(self, chunks: Dict[str, List[DocumentChunk]], namespace: str) -> List[str]:
        """
        Takes in a dict from document id to list of document chunks and inserts them into the index.
        Return a list of document ids.
        """
        # Initialize a list of ids to return
        doc_ids: List[str] = []
        vector_ids: List[str] = []
        # Initialize a list of vectors to upsert
        vectors = []
        # Loop through the dict items
        for doc_id, chunk_list in chunks.items():
            # Append the id to the ids list
            doc_ids.append(doc_id)
            logger.debug(f"Upserting document_id: {doc_id}")
            for chunk in chunk_list:
                # Create a vector tuple of (id, embedding, metadata)
                # Convert the metadata object to a dict with unix timestamps for dates
                pinecone_metadata = self._get_pinecone_metadata(chunk.metadata)
                # Add the text and document id to the metadata dict
                pinecone_metadata["text"] = chunk.text
                pinecone_metadata["document_id"] = doc_id
                vector = (chunk.id, chunk.embedding, pinecone_metadata)
                vectors.append(vector)
                vector_ids.append(chunk.id)

        # Split the vectors list into batches of the specified size
        batches = [
            vectors[i : i + UPSERT_BATCH_SIZE]
            for i in range(0, len(vectors), UPSERT_BATCH_SIZE)
        ]
        # Upsert each batch to Pinecone
        for batch in batches:
            try:
                logger.debug(f"Upserting batch of size {len(batch)} to namespace {namespace}")
                self.index.upsert(vectors=batch, namespace=namespace)
                logger.debug(f"Upserted batch successfully")
            except Exception as e:
                logger.error(e);
                logger.error(f"Error upserting batch: {e}")
                raise e

        return vector_ids

    @retry(wait=wait_random_exponential(min=1, max=20), stop=stop_after_attempt(3))
    async def _query(
        self,        
        queries: List[QueryWithEmbedding],
        namespace: str
    ) -> List[QueryResult]:
        """
        Takes in a list of queries with embeddings and filters and returns a list of query results with matching document chunks and scores.
        """

        # Define a helper coroutine that performs a single query and returns a QueryResult
        async def _single_query(query: QueryWithEmbedding) -> QueryResult:            
            # Convert the metadata filter object to a dict with pinecone filter expressions
            pinecone_filter = self._get_pinecone_filter(query.filter)

            try:            
                # Query the index with the query embedding, filter, and top_k
                query_response = self.index.query(
                    namespace=namespace,
                    top_k=query.top_k,
                    vector=query.embedding,
                    filter=pinecone_filter,
                    include_metadata=True,
                )                
                
            except Exception as e:
                logger.error(f"Error querying index: {e}")
                raise e

            query_results: List[DocumentChunkWithScore] = []
            for result in query_response.matches:
                score = result.score
                metadata = result.metadata
                # Remove document id and text from metadata and store it in a new variable
                metadata_without_text = (
                    {key: value for key, value in metadata.items() if key != "text"}
                    if metadata
                    else None
                )

                # If the source is not a valid Source in the Source enum, set it to None
                if (
                    metadata_without_text
                    and "source" in metadata_without_text
                    and metadata_without_text["source"] not in Source.__members__
                ):
                    metadata_without_text["source"] = None

                # Create a document chunk with score object with the result data
                result = DocumentChunkWithScore(
                    id=result.id,
                    score=score,
                    text=str(metadata["text"])
                    if metadata and "text" in metadata
                    else "",
                    metadata=metadata_without_text,
                )
                
                query_results.append(result)
            return QueryResult(query=query.query, results=query_results)

        # Use asyncio.gather to run multiple _single_query coroutines concurrently and collect their results
        results: List[QueryResult] = await asyncio.gather(
            *[_single_query(query) for query in queries]
        )

        logger.info(f"Results: {len(results)}")

        return results

    @retry(wait=wait_random_exponential(min=1, max=20), stop=stop_after_attempt(3))
    async def delete(
        self,
        ids: Optional[List[str]] = None,
        filter: Optional[DocumentMetadataFilter] = None,
        delete_all: Optional[bool] = None,
        namespace: Optional[str] = None
    ) -> bool:
        logger.info('deleting vectors')
        """
        Removes vectors by ids, filter, or everything from the index.
        """
        # Delete all vectors from the index if delete_all is True
        if delete_all:
            try:
                logger.debug(f"Deleting all vectors from index")
                self.index.delete(delete_all=True, namespace=namespace)
                logger.debug(f"Deleted all vectors successfully")
                return True
            except Exception as e:
                logger.error(f"Error deleting all vectors: {e}")
                raise e

        # Convert the metadata filter object to a dict with pinecone filter expressions
        pinecone_filter = self._get_pinecone_filter(filter)
        # Delete vectors that match the filter from the index if the filter is not empty
        if pinecone_filter != {}:            
            try:
                logger.info(f"Deleting vectors with filter {pinecone_filter}")
                self.index.delete(filter=pinecone_filter, namespace=namespace)
                logger.info(f"Deleted vectors with filter successfully")
            except Exception as e:
                logger.error(f"Error deleting vectors with filter: {e}")
                raise e

        # Delete vectors that match the document ids from the index if the ids list is not empty
        if ids is not None and len(ids) > 0:
            try:
                logger.info(f"Deleting vectors with ids {ids}")
                pinecone_filter = {"document_id": {"$in": ids}}
                self.index.delete(ids=ids, namespace=namespace)  
                logger.info(f"Deleted vectors with ids successfully")
            except Exception as e:
                logger.error(f"Error deleting vectors with ids: {e}")
                raise e

        return True

    def _get_pinecone_filter(
        self, filter: Optional[DocumentMetadataFilter] = None
    ) -> Dict[str, Any]:
        if filter is None:
            return {}

        pinecone_filter = {}

        # For each field in the MetadataFilter, check if it has a value and add the corresponding pinecone filter expression
        # For start_date and end_date, uses the $gte and $lte operators respectively
        # For other fields, uses the $eq operator
        for field, value in filter.dict().items():
            if value is not None:
                if field == "start_date":
                    pinecone_filter["created_at"] = pinecone_filter.get(
                        "created_at", {}
                    )
                    pinecone_filter["created_at"]["$gte"] = to_unix_timestamp(value)
                elif field == "end_date":
                    pinecone_filter["created_at"] = pinecone_filter.get(
                        "created_at", {}
                    )
                    pinecone_filter["created_at"]["$lte"] = to_unix_timestamp(value)
                else:
                    pinecone_filter[field] = value

        return pinecone_filter

    def _get_pinecone_metadata(
        self, metadata: Optional[DocumentChunkMetadata] = None
    ) -> Dict[str, Any]:
        if metadata is None:
            return {}

        pinecone_metadata = {}

        # For each field in the Metadata, check if it has a value and add it to the pinecone metadata dict
        # For fields that are dates, convert them to unix timestamps
        for field, value in metadata.dict().items():
            if value is not None:
                if field in ["created_at"]:
                    pinecone_metadata[field] = to_unix_timestamp(value)
                else:
                    pinecone_metadata[field] = value

        return pinecone_metadata
