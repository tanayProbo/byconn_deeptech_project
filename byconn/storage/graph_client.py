import logging
from typing import List, Dict, Any, Optional

try:
    from neo4j import GraphDatabase, Driver
    NEO4J_AVAILABLE = True
except ImportError:
    NEO4J_AVAILABLE = False

logger = logging.getLogger("byconnx.storage.neo4j")


class Neo4jGraphAdapter:
    """
    Neo4j Database client adapter.
    Constructs and links extracted entities and predicate relations to formulate Knowledge Graphs.
    """
    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "password"
    ):
        self.uri = uri
        self.auth = (user, password)
        self.driver: Optional["Driver"] = None

    def connect(self):
        """Initializes the Neo4j driver connection."""
        if not NEO4J_AVAILABLE:
            raise ImportError("neo4j driver not installed. Run: pip install neo4j")
        self.driver = GraphDatabase.driver(self.uri, auth=self.auth)
        self.driver.verify_connectivity()
        logger.info(f"Connected to Neo4j at {self.uri}")

    def _get_driver(self) -> "Driver":
        if self.driver is None:
            self.connect()
        return self.driver

    def write_triple(
        self,
        subject: str,
        predicate: str,
        obj: str,
        subject_type: str = "Entity",
        object_type: str = "Entity"
    ):
        """Inserts entity nodes and their predicate relationship into Neo4j via Cypher."""
        safe_predicate = predicate.upper().replace(" ", "_").replace("-", "_")
        cypher = (
            f"MERGE (s:{subject_type} {{name: $subject}}) "
            f"MERGE (o:{object_type} {{name: $obj}}) "
            f"MERGE (s)-[r:{safe_predicate}]->(o) "
            f"RETURN s, r, o"
        )
        with self._get_driver().session() as session:
            session.run(cypher, subject=subject, obj=obj)
        logger.debug(f"Neo4j: Written ({subject})-[:{safe_predicate}]->({obj})")

    def batch_write_triples(self, triples: List[Dict[str, str]]):
        """Inserts multiple relation triples in a single transaction."""
        logger.info(f"Neo4j: Batch writing {len(triples)} triples...")
        with self._get_driver().session() as session:
            with session.begin_transaction() as tx:
                for triple in triples:
                    safe_pred = triple["predicate"].upper().replace(" ", "_").replace("-", "_")
                    cypher = (
                        f"MERGE (s:Entity {{name: $subject}}) "
                        f"MERGE (o:Entity {{name: $obj}}) "
                        f"MERGE (s)-[r:{safe_pred}]->(o)"
                    )
                    tx.run(cypher, subject=triple["subject"], obj=triple["object"])
        logger.info(f"Neo4j: Committed {len(triples)} triples successfully.")

    def query(self, cypher: str, params: Optional[Dict] = None) -> List[Dict]:
        """Runs an arbitrary read Cypher query and returns rows as dicts."""
        with self._get_driver().session() as session:
            result = session.run(cypher, parameters=params or {})
            return [dict(record) for record in result]

    def close(self):
        """Closes the Neo4j driver."""
        if self.driver:
            self.driver.close()
            logger.info("Neo4j connection closed.")
