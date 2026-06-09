"""
New database functions to append to database_v2.py
"""

new_functions = '''

# ===== Health Tracking & Compliance =====

async def get_compliance_rate(patient_id: str, days: int = 7) -> Dict[str, Any]:
    """Calculate medicine adherence rate (taken/total) for the past N days."""
    try:
        async with connect_db() as db:
            # Get all intake logs for this patient in past N days
            cutoff_date = (
                "datetime('now', '-" + str(days) + " days')"
            )
            query = f"""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'taken' THEN 1 ELSE 0 END) as taken,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) as skipped,
                    SUM(CASE WHEN status = 'missed' THEN 1 ELSE 0 END) as missed
                FROM intake_logs
                WHERE medicine_id IN (SELECT id FROM medicines WHERE patient_id = ?)
                  AND datetime(timestamp) >= {cutoff_date}
            """
            async with db.execute(query, (patient_id,)) as cursor:
                row = await cursor.fetchone()
            
            if not row:
                return {'total': 0, 'taken': 0, 'skipped': 0, 'missed': 0, 'compliance_rate': 0.0}
            
            total = row['total'] or 0
            taken = row['taken'] or 0
            skipped = row['skipped'] or 0
            missed = row['missed'] or 0
            
            compliance_rate = (taken / total * 100) if total > 0 else 0.0
            
            return {
                'total': total,
                'taken': taken,
                'skipped': skipped,
                'missed': missed,
                'compliance_rate': round(compliance_rate, 1)
            }
    except Exception:
        logger.exception('Failed to get compliance rate for patient %s', patient_id)
        return {'total': 0, 'taken': 0, 'skipped': 0, 'missed': 0, 'compliance_rate': 0.0}


async def check_drug_interactions(patient_id: str) -> List[Dict[str, Any]]:
    """Check if patient's medicines have dangerous interactions."""
    try:
        async with connect_db() as db:
            # Get patient's medicine names
            query = "SELECT DISTINCT name FROM medicines WHERE patient_id = ?"
            async with db.execute(query, (patient_id,)) as cursor:
                med_rows = await cursor.fetchall()
            
            patient_meds = [m['name'] for m in med_rows]
            if len(patient_meds) < 2:
                return []
            
            # Check for interactions between pairs
            interactions = []
            for i, drug1 in enumerate(patient_meds):
                for drug2 in patient_meds[i+1:]:
                    # Query drug_interactions table
                    query = """
                        SELECT * FROM drug_interactions 
                        WHERE (drug1 = ? AND drug2 = ?) 
                           OR (drug1 = ? AND drug2 = ?)
                    """
                    async with db.execute(query, (drug1, drug2, drug2, drug1)) as cursor:
                        inter = await cursor.fetchone()
                    
                    if inter:
                        interactions.append({
                            'drug1': inter['drug1'],
                            'drug2': inter['drug2'],
                            'severity': inter['severity'],
                            'description': inter['description']
                        })
            
            return interactions
    except Exception:
        logger.exception('Failed to check drug interactions for patient %s', patient_id)
        return []


async def add_health_reading(patient_id: str, reading_type: str, value: float, unit: str, notes: str = '') -> bool:
    """Record a health reading (BP, glucose, weight, pulse)."""
    try:
        async with connect_db() as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.execute(
                """
                INSERT INTO health_readings (patient_id, reading_type, value, unit, notes, recorded_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (patient_id, reading_type, value, unit, notes)
            )
            await db.commit()
            return True
    except Exception:
        logger.exception('Failed to add health reading for patient %s', patient_id)
        return False


async def get_health_readings(patient_id: str, reading_type: str = None, limit: int = 30) -> List[Dict[str, Any]]:
    """Get health readings for a patient, optionally filtered by type."""
    try:
        async with connect_db() as db:
            if reading_type:
                query = """
                    SELECT * FROM health_readings 
                    WHERE patient_id = ? AND reading_type = ?
                    ORDER BY recorded_at DESC LIMIT ?
                """
                async with db.execute(query, (patient_id, reading_type, limit)) as cursor:
                    rows = await cursor.fetchall()
            else:
                query = """
                    SELECT * FROM health_readings 
                    WHERE patient_id = ?
                    ORDER BY recorded_at DESC LIMIT ?
                """
                async with db.execute(query, (patient_id, limit)) as cursor:
                    rows = await cursor.fetchall()
            
            return [dict(row) for row in rows] if rows else []
    except Exception:
        logger.exception('Failed to get health readings for patient %s', patient_id)
        return []


async def add_chronic_disease(patient_id: str, disease_name: str, notes: str = '') -> bool:
    """Record a chronic disease for a patient."""
    try:
        async with connect_db() as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.execute(
                """
                INSERT INTO chronic_diseases (patient_id, disease_name, diagnosis_date, notes)
                VALUES (?, ?, datetime('now'), ?)
                """,
                (patient_id, disease_name, notes)
            )
            await db.commit()
            return True
    except Exception:
        logger.exception('Failed to add chronic disease for patient %s', patient_id)
        return False


async def get_patient_diseases(patient_id: str) -> List[Dict[str, Any]]:
    """Get chronic diseases for a patient."""
    try:
        async with connect_db() as db:
            query = "SELECT * FROM chronic_diseases WHERE patient_id = ? ORDER BY diagnosis_date DESC"
            async with db.execute(query, (patient_id,)) as cursor:
                rows = await cursor.fetchall()
            return [dict(row) for row in rows] if rows else []
    except Exception:
        logger.exception('Failed to get diseases for patient %s', patient_id)
        return []


async def add_doctor_appointment(patient_id: str, appointment_date: str, doctor_name: str, reason: str, notes: str = '') -> bool:
    """Record a doctor appointment."""
    try:
        async with connect_db() as db:
            await db.execute("PRAGMA foreign_keys = ON;")
            await db.execute(
                """
                INSERT INTO doctor_appointments (patient_id, appointment_date, doctor_name, reason, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (patient_id, appointment_date, doctor_name, reason, notes)
            )
            await db.commit()
            return True
    except Exception:
        logger.exception('Failed to add appointment for patient %s', patient_id)
        return False


async def get_upcoming_appointments(patient_id: str) -> List[Dict[str, Any]]:
    """Get upcoming doctor appointments for a patient."""
    try:
        async with connect_db() as db:
            query = """
                SELECT * FROM doctor_appointments 
                WHERE patient_id = ? AND datetime(appointment_date) > datetime('now')
                ORDER BY appointment_date ASC
            """
            async with db.execute(query, (patient_id,)) as cursor:
                rows = await cursor.fetchall()
            return [dict(row) for row in rows] if rows else []
    except Exception:
        logger.exception('Failed to get appointments for patient %s', patient_id)
        return []


async def seed_drug_interactions() -> None:
    """Pre-populate common drug interactions."""
    try:
        async with connect_db() as db:
            # Check if already seeded
            async with db.execute("SELECT COUNT(*) as cnt FROM drug_interactions") as cursor:
                result = await cursor.fetchone()
                if result['cnt'] > 0:
                    return
            
            interactions = [
                ('Warfarin', 'Aspirin', 'high', 'قد يزيد خطر النزف الداخلي بشكل خطير'),
                ('Metformin', 'Contrast', 'high', 'خطر الفشل الكلوي - تجنب التباين قبل 48 ساعة'),
                ('ACE Inhibitor', 'Potassium', 'medium', 'قد يرفع مستوى البوتاسيوم بشكل خطير'),
                ('Statins', 'Grapefruit', 'medium', 'الجريب فروت يزيد تركيز الدواء في الدم'),
                ('NSAIDs', 'Lithium', 'high', 'قد يزيد سمية الليثيوم'),
                ('SSRIs', 'Tramadol', 'medium', 'خطر متلازمة السيروتونين'),
            ]
            
            await db.executemany(
                """
                INSERT OR IGNORE INTO drug_interactions (drug1, drug2, severity, description)
                VALUES (?, ?, ?, ?)
                """,
                interactions
            )
            await db.commit()
            logger.info('Drug interactions seeded successfully')
    except Exception:
        logger.exception('Failed to seed drug interactions')
'''

print(new_functions)
