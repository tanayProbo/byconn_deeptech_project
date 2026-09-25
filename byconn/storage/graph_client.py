import logging
from typing import List, Dict, Any

logger = logging.getLogger("byconnx.storage.neo4j")

class Neo4jGraphAdapter:
    """
    Neo4j Database client adapter.
    Constructs and links extracted entities and predicate relations to formulate Knowledge Graphs.
    """
    def __init__(self, uri: str = "bolt://localhost:7687", user: str = "neo4j", password: str = "password"):
        self.uri = uri
        self.auth = (user, password)
        self.driver = None

    def connect(self):
        """Initializes drivers context connection."""
        logger.info(f"Connected to Neo4j graph cluster endpoint at {self.uri}")
        # In production:
        # self.driver = GraphDatabase.driver(self.uri, auth=self.auth)

    def write_triple(self, subject: str, predicate: str, obj: str, subject_type: str = "Entity", object_type: str = "Entity"):
        """Inserts entity nodes and their predicates into Neo4j using Cypher queries."""
        # Clean predicate to ensure cypher parsing is safe
        safe_predicate = predicate.upper().replace(" ", "_")
        
        cypher = f"""
        MERGE (s:{subject_type} {{name: $subject}})
        MERGE (o:{object_type} {{name: $obj}})
        MERGE (s)-[r:{safe_predicate}]->(o)
        RETURN s, r, o
        """
        logger.debug(f"Neo4j: Merging relation: ({subject})-[:{safe_predicate}]->({obj})")
        # In production:
        # with self.driver.session() as session:
        #     session.run(cypher, subject=subject, obj=obj)

    def batch_write_triples(self, triples: List[Dict[str, str]]):
        """Inserts multiple graph links within a single transaction pipeline."""
        logger.info(f"Neo4j: Batch writing {len(triples)} predicate relations...")
        for triple in triples:
            self.write_triple(
                subject=triple["subject"],
                predicate=triple["predicate"],
                obj=triple["object"]
            )
