"""
Graph Updater — consumes supply-chain.canonical from Kafka,
extracts relationships, writes to Neo4j knowledge graph.
"""
import json
import os
from datetime import datetime, timezone

from kafka import KafkaConsumer, KafkaProducer
from neo4j import GraphDatabase

from kafka_config import kafka_kwargs

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "kafka.ai-data:9092")
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://neo4j.ai-data:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "rajailab")

producer = KafkaProducer(
    **kafka_kwargs(),
    value_serializer=lambda v: json.dumps(v).encode("utf-8")
)

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def now():
    return datetime.now(timezone.utc).isoformat()

def publish(topic, message):
    producer.send(topic, message)
    producer.flush()

def write_triples(tx, triples):
    """Write relationship triples to Neo4j using MERGE (idempotent)"""
    for triple in triples:
        subj = triple.get("subject", "")
        pred = triple.get("predicate", "")
        obj = triple.get("object", "")

        if not all([subj, pred, obj]):
            continue

        # MERGE creates nodes/relationships if they don't exist
        query = f"""
        MERGE (a:Entity {{id: $subject}})
        MERGE (b:Entity {{id: $object}})
        MERGE (a)-[r:{pred}]->(b)
        SET r.updated_at = $timestamp
        """
        tx.run(query, subject=subj, object=obj, timestamp=now())

def write_supplier(tx, supplier_data):
    """Write supplier node with properties"""
    query = """
    MERGE (s:Supplier {id: $id})
    SET s.name = $name,
        s.updated_at = $timestamp
    """
    tx.run(query,
        id=supplier_data.get("id", "unknown"),
        name=supplier_data.get("name", "unknown"),
        timestamp=now()
    )
    # Write identifiers
    for ident in supplier_data.get("identifiers", []):
        id_query = """
        MERGE (s:Supplier {id: $supplier_id})
        MERGE (i:Identifier {system: $system, value: $value})
        MERGE (s)-[:IDENTIFIED_BY]->(i)
        """
        tx.run(id_query,
            supplier_id=supplier_data.get("id"),
            system=ident.get("system", ""),
            value=ident.get("value", "")
        )

def write_parts(tx, parts, supplier_id=None):
    """Write part nodes and link to supplier"""
    for part in parts:
        query = """
        MERGE (p:Part {partNumber: $partNumber})
        SET p.description = $description,
            p.updated_at = $timestamp
        """
        tx.run(query,
            partNumber=part.get("partNumber", "unknown"),
            description=part.get("description", ""),
            timestamp=now()
        )
        if supplier_id:
            link_query = """
            MATCH (s:Supplier {id: $supplier_id})
            MATCH (p:Part {partNumber: $partNumber})
            MERGE (s)-[:SUPPLIES]->(p)
            """
            tx.run(link_query, supplier_id=supplier_id, partNumber=part.get("partNumber"))

def write_shipment(tx, shipment_data, supplier_id=None, parts=None):
    """Write shipment node with status"""
    query = """
    MERGE (sh:Shipment {id: $id})
    SET sh.status = $status,
        sh.original_eta = $original_eta,
        sh.revised_eta = $revised_eta,
        sh.delay_days = $delay_days,
        sh.updated_at = $timestamp
    """
    tx.run(query,
        id=shipment_data.get("id", "unknown"),
        status=shipment_data.get("status", "unknown"),
        original_eta=shipment_data.get("original_eta", ""),
        revised_eta=shipment_data.get("revised_eta", ""),
        delay_days=shipment_data.get("delay_days", 0),
        timestamp=now()
    )
    if supplier_id:
        tx.run("""
            MATCH (sh:Shipment {id: $shipment_id})
            MATCH (s:Supplier {id: $supplier_id})
            MERGE (sh)-[:FROM]->(s)
        """, shipment_id=shipment_data.get("id"), supplier_id=supplier_id)
    if parts:
        for part in parts:
            tx.run("""
                MATCH (sh:Shipment {id: $shipment_id})
                MATCH (p:Part {partNumber: $partNumber})
                MERGE (sh)-[:CONTAINS]->(p)
            """, shipment_id=shipment_data.get("id"), partNumber=part.get("partNumber"))

def process_canonical_event(event):
    """Process a canonical supply chain event into Neo4j"""
    entities = event.get("entities", {})
    transaction = event.get("transaction", {})
    graph_triples = event.get("graph", [])

    with driver.session() as session:
        # Write explicit graph triples if present
        if graph_triples:
            session.execute_write(write_triples, graph_triples)

        # Write supplier
        supplier = entities.get("supplier", {})
        if supplier:
            session.execute_write(write_supplier, supplier)

        # Write parts and link to supplier
        parts = entities.get("parts", [])
        if parts:
            session.execute_write(write_parts, parts, supplier.get("id"))

        # Write shipment
        shipment = transaction.get("shipment", {})
        if shipment.get("id"):
            session.execute_write(write_shipment, shipment, supplier.get("id"), parts)

def seed_sample_data():
    """Seed the graph with sample supply chain data"""
    print("[graph-updater] Seeding sample data...")
    with driver.session() as session:
        session.run("""
            // Suppliers
            MERGE (s1:Supplier {id: 'sup-7721', name: 'Acme Bearings Co'})
            MERGE (s2:Supplier {id: 'sup-3302', name: 'GlobalBearings Inc'})
            MERGE (s3:Supplier {id: 'sup-5581', name: 'PrecisionParts Ltd'})

            // Parts
            MERGE (p1:Part {partNumber: 'BRG-7721-A', description: 'Tapered roller bearing 50mm'})
            MERGE (p2:Part {partNumber: 'MTR-4400-B', description: 'AC Motor 5HP'})
            MERGE (p3:Part {partNumber: 'GKT-9900-C', description: 'Gasket set industrial'})

            // Products
            MERGE (pr1:Product {id: 'prod-9921', name: 'Model X Turbine Assembly'})
            MERGE (pr2:Product {id: 'prod-9934', name: 'Model X-Pro Turbine Assembly'})

            // Production lines
            MERGE (l1:Line {id: 'LINE-4', name: 'Production Line 4'})
            MERGE (l2:Line {id: 'LINE-7', name: 'Production Line 7'})

            // Facility
            MERGE (f1:Facility {id: 'fac-slc-01', name: 'Salt Lake City Plant 1'})

            // Relationships
            MERGE (s1)-[:SUPPLIES]->(p1)
            MERGE (s2)-[:SUPPLIES]->(p1)
            MERGE (s3)-[:SUPPLIES]->(p1)
            MERGE (s1)-[:SUPPLIES]->(p2)

            MERGE (p1)-[:USED_IN]->(pr1)
            MERGE (p1)-[:USED_IN]->(pr2)
            MERGE (p2)-[:USED_IN]->(pr1)

            MERGE (pr1)-[:PRODUCED_ON]->(l1)
            MERGE (pr2)-[:PRODUCED_ON]->(l2)

            MERGE (l1)-[:LOCATED_AT]->(f1)
            MERGE (l2)-[:LOCATED_AT]->(f1)
        """)
    print("[graph-updater] Sample data seeded")

def consume_loop():
    print(f"[graph-updater] Connecting to Neo4j at {NEO4J_URI}")
    driver.verify_connectivity()
    print("[graph-updater] Neo4j connected")

    # Seed sample data on startup
    seed_sample_data()

    print(f"[graph-updater] Starting consumer on {KAFKA_BOOTSTRAP}")
    consumer = KafkaConsumer(
        "supply-chain.canonical",
        **kafka_kwargs(),
        value_deserializer=lambda m: json.loads(m.decode("utf-8")),
        group_id="graph-updater-group",
        auto_offset_reset="earliest"
    )

    print("[graph-updater] Listening on supply-chain.canonical...")
    for message in consumer:
        event = message.value
        event_type = event.get("event", {}).get("type", "unknown")
        event_id = event.get("event", {}).get("id", "unknown")

        print(f"[graph-updater] Processing: {event_type} ({event_id})")

        try:
            process_canonical_event(event)
            print(f"[graph-updater] Written to Neo4j: {event_id}")
        except Exception as e:
            print(f"[graph-updater] ERROR: {e}")

if __name__ == "__main__":
    consume_loop()
