"""
Supply Chain Demo — Seeds documents, Neo4j graph, and inventory data.
Run this to set up the full scenario before running the agent.
"""
import httpx
import json
import time
import sys

RAG_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
TENANT = "acme-corp"

print(f"=== Seeding Supply Chain Data for tenant: {TENANT} ===")
print(f"RAG endpoint: {RAG_URL}")
print()

# --- Document 1: Equipment Maintenance Manual ---
docs = [
    {
        "source": "model-x-turbine-maintenance-manual.pdf",
        "text": """Model X Industrial Turbine — Maintenance Manual v4.2

Chapter 1: Overview
The Model X turbine is a high-performance industrial turbine designed for continuous operation in heavy manufacturing environments. Operating temperature range: -20°C to 85°C. Maximum RPM: 12,000. Weight: 2,400 kg. Expected lifetime: 25,000 operating hours with proper maintenance.

Chapter 2: Critical Components
The main bearing assembly uses a tapered roller bearing (SKF-32210). This is the most critical wear component. The bearing must be replaced every 5,000 operating hours or immediately if vibration exceeds 4.5 mm/s.

Chapter 3: Bearing Replacement Procedure
1. Shut down turbine and allow to cool for minimum 4 hours
2. Remove housing cover (12 bolts, M16)
3. Extract old bearing using puller tool (Part: TOOL-BRG-001)
4. Clean bearing seat with isopropyl alcohol
5. Install new bearing (SKF-32210) with press-fit tool
6. Apply torque: 85 Nm (+/- 5%). DO NOT exceed 90 Nm to avoid housing damage
7. Apply Mobilgrease XHP 222 to bearing surfaces
8. Replace housing cover, torque bolts to 45 Nm in star pattern
9. Run at 50% RPM for 30 minutes, check vibration levels
10. If vibration < 2.0 mm/s, clear for full operation

Chapter 4: Lubrication Schedule
Use ISO VG 68 synthetic oil for main gearbox. Check oil levels weekly. Full oil change every 2,000 hours. Oil temperature should not exceed 80°C during normal operation. If temperature exceeds 95°C, shut down immediately.

Chapter 5: Troubleshooting
Problem: Excessive vibration (>4.5 mm/s)
- Check bearing alignment first
- Common causes: worn bearings, misalignment, unbalanced rotor
- If vibration persists after bearing replacement, check shaft runout

Problem: Overheating (>95°C)
- Check cooling system flow rate
- Verify oil level and quality
- Check for blocked cooling fins
- Inspect thermal paste on heat exchangers

Chapter 6: Emergency Procedures
If smoke or unusual odor is detected:
1. Activate emergency stop immediately
2. Evacuate personnel from 10m radius
3. Do NOT attempt to open housing
4. Contact maintenance team and fire safety
5. Document time, conditions, and observations

Table A: Torque Specifications
Model X bearing: 85 Nm
Model X housing bolts: 45 Nm
Model X gearbox drain: 25 Nm
Model Y bearing: 110 Nm
Model Z bearing: 65 Nm
All values +/- 5% with calibrated torque wrench."""
    },
    {
        "source": "supplier-catalog-2026.pdf",
        "text": """Myriad Industrial Supply — Supplier Catalog 2026

Bearings Division:

SKF-32210 Tapered Roller Bearing
- Dimensions: OD 90mm, ID 50mm, Width 23mm
- Weight: 0.68 kg
- Max RPM: 15,000
- Operating temp: -30°C to 120°C
- Price: $3.50/unit (1000+ qty), $4.20/unit (100-999), $5.80/unit (<100)
- Lead time: 2-5 business days depending on supplier

Approved Suppliers for SKF-32210:
1. Acme Bearings Co (Primary) — Lead time: 3 days, Price: $3.50, Rating: 94% on-time
2. GlobalBearings Inc (Secondary) — Lead time: 2 days, Price: $4.20, Rating: 97% on-time
3. PrecisionParts Ltd (Tertiary) — Lead time: 5 days, Price: $3.80, Rating: 89% on-time

Motors Division:

AC Motor 5HP (MTR-4400-B)
- Voltage: 380-480V, 3-phase
- RPM: 1,750
- Frame: 184T
- Price: $120/unit
- Lead time: 3-7 business days
- Supplier: MotorWorld (sole source)

Gaskets Division:

Industrial Gasket Set (GKT-9900-C)
- Material: Aramid fiber with NBR binder
- Temperature range: -40°C to 250°C
- Price: $8.50/set
- Lead time: 1-2 business days
- Suppliers: GasketKing, SealMaster"""
    },
    {
        "source": "production-schedule-q2-2026.pdf",
        "text": """Production Schedule — Q2 2026 (April-June)

Salt Lake City Plant 1

Production Line 4 (LINE-4):
- Product: Model X Turbine Assembly (PROD-9921)
- Daily output: 80 units
- Shift: 24/7 operation, 3 shifts
- Critical parts: SKF-32210 bearing (2 per unit = 160/day), MTR-4400-B motor (1 per unit)
- Current inventory: 1,250 bearings (7.8 days supply), 340 motors (4.25 days supply)
- Safety stock target: 10 days supply

Production Line 7 (LINE-7):
- Product: Model X-Pro Turbine Assembly (PROD-9934)
- Daily output: 40 units
- Shift: 2 shifts (Mon-Fri)
- Critical parts: SKF-32210 bearing (4 per unit = 160/day), GKT-9900-C gasket (2 per unit)
- Current inventory: same bearing pool as LINE-4

Combined bearing consumption: 320 bearings/day
Combined bearing inventory: 1,250 units = 3.9 days supply
WARNING: Below 10-day safety stock target

Revenue impact per day of downtime:
- LINE-4: 80 units × $12,000 = $960,000/day
- LINE-7: 40 units × $18,000 = $720,000/day
- Combined: $1,680,000/day potential revenue loss"""
    },
    {
        "source": "quality-report-march-2026.pdf",
        "text": """Quality Report — March 2026

Bearing Failure Analysis:
- 3 bearing failures in March (above normal rate of 1/month)
- Root cause: 2 failures traced to Lot #BRG-2026-0112 from Acme Bearings
- 1 failure due to improper installation (torque exceeded 95 Nm)
- Recommendation: Quarantine remaining units from Lot #BRG-2026-0112
- Quarantined: 200 units removed from inventory

Supplier Quality Scores (March):
- Acme Bearings: 91% (down from 94%, due to lot issue)
- GlobalBearings: 99% (no quality incidents)
- PrecisionParts: 92% (1 late delivery)
- MotorWorld: 100% (no issues)

Actions Taken:
1. Issued corrective action request to Acme Bearings (CAR-2026-0034)
2. Increased incoming inspection rate for bearings from 5% to 15%
3. Updated torque verification checklist for bearing installation
4. Scheduled additional operator training for April"""
    }
]

print(f"Ingesting {len(docs)} documents...")
for doc in docs:
    try:
        r = httpx.post(f"{RAG_URL}/ingest",
            json={"source": doc["source"], "text": doc["text"], "tenant_id": TENANT},
            timeout=60)
        result = r.json()
        print(f"  ✓ {doc['source']} → {result.get('doc_id', '?')} ({result.get('status', '?')})")
    except Exception as e:
        print(f"  ✗ {doc['source']} → Error: {e}")

print()
print("=== Supply Chain Data Seeded ===")
print(f"Tenant: {TENANT}")
print(f"Documents: {len(docs)}")
print(f"Topics: maintenance manual, supplier catalog, production schedule, quality report")
print()
print("Ready for agent scenario:")
print('  curl -X POST http://localhost:8001/agent/run \\')
print('    -H "Content-Type: application/json" \\')
print('    -d \'{"question": "Acme Bearings shipment of 500 SKF-32210 bearings is delayed 5 days. We have 3.9 days of supply. What should we do?"}\'')
