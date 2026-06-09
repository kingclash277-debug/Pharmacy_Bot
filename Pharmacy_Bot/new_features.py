# New database functions for advanced features
# Add these to database_v2.py at the end

async def get_compliance_rate(patient_id: str, days: int = 7) -> Dict[str, Any]:
    """Calculate compliance rate for a patient over the past N days."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        # Get taken + skipped + total logs in the last N days
        query = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'taken' THEN 1 ELSE 0 END) as taken,
                SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) as skipped,
                SUM(CASE WHEN status = 'missed' THEN 1 ELSE 0 END) as missed
            FROM intake_logs il
            JOIN medicines m ON m.id = il.medicine_id
            WHERE m.patient_id = ? AND il.timestamp >= datetime('now', '-' || ? || ' days')
        """
        async with db.execute(query, (patient_id, days)) as cursor:
            row = await cursor.fetchone()
            if not row or row['total'] == 0:
                return {'total': 0, 'taken': 0, 'skipped': 0, 'missed': 0, 'compliance_rate': 0}
            
            total = row['total']
            taken = row['taken'] or 0
            rate = (taken / total * 100) if total > 0 else 0
            return {
                'total': total,
                'taken': taken,
                'skipped': row['skipped'] or 0,
                'missed': row['missed'] or 0,
                'compliance_rate': round(rate, 2)
            }


async def check_drug_interactions(patient_id: str) -> List[Dict[str, Any]]:
    """Check for interactions between patient's medicines."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        # Get all medicines for the patient
        query = "SELECT id, name FROM medicines WHERE patient_id = ?"
        async with db.execute(query, (patient_id,)) as cursor:
            medicines = await cursor.fetchall()
        
        medicines_list = [_row_to_dict(m) for m in medicines]
        interactions = []
        
        # Check for interactions between each pair
        for i in range(len(medicines_list)):
            for j in range(i + 1, len(medicines_list)):
                drug1 = medicines_list[i]['name'].lower()
                drug2 = medicines_list[j]['name'].lower()
                
                # Look up in interactions table
                q = """
                    SELECT * FROM drug_interactions 
                    WHERE (LOWER(drug1) = ? AND LOWER(drug2) = ?) 
                       OR (LOWER(drug1) = ? AND LOWER(drug2) = ?)
                """
                async with db.execute(q, (drug1, drug2, drug2, drug1)) as cursor:
                    interaction = await cursor.fetchone()
                    if interaction:
                        interactions.append({
                            'drug1': medicines_list[i]['name'],
                            'drug2': medicines_list[j]['name'],
                            'severity': interaction['severity'],
                            'description': interaction['description']
                        })
        
        return interactions


async def add_health_reading(patient_id: str, reading_type: str, value: float, unit: str = '', notes: str = '') -> int:
    """Record a health reading (BP, glucose, weight, etc)."""
    async with connect_db() as db:
        cursor = await db.execute(
            """INSERT INTO health_readings(patient_id, reading_type, value, unit, notes) 
               VALUES (?, ?, ?, ?, ?)""",
            (patient_id, reading_type, value, unit, notes),
        )
        await db.commit()
        return cursor.lastrowid


async def get_health_readings(patient_id: str, reading_type: str, limit: int = 30) -> List[Dict[str, Any]]:
    """Get recent health readings of a specific type."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT * FROM health_readings 
            WHERE patient_id = ? AND reading_type = ? 
            ORDER BY recorded_at DESC LIMIT ?
        """
        async with db.execute(query, (patient_id, reading_type, limit)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def add_chronic_disease(patient_id: str, disease_name: str, diagnosis_date: str = None, notes: str = '') -> int:
    """Add a chronic disease to patient profile."""
    async with connect_db() as db:
        cursor = await db.execute(
            """INSERT INTO chronic_diseases(patient_id, disease_name, diagnosis_date, notes) 
               VALUES (?, ?, ?, ?)""",
            (patient_id, disease_name, diagnosis_date, notes),
        )
        await db.commit()
        return cursor.lastrowid


async def get_patient_diseases(patient_id: str) -> List[Dict[str, Any]]:
    """Get all chronic diseases for a patient."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM chronic_diseases WHERE patient_id = ?"
        async with db.execute(query, (patient_id,)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def add_doctor_appointment(patient_id: str, appointment_date: str, doctor_name: str = '', reason: str = '', notes: str = '') -> int:
    """Add a doctor appointment reminder."""
    async with connect_db() as db:
        cursor = await db.execute(
            """INSERT INTO doctor_appointments(patient_id, appointment_date, doctor_name, reason, notes) 
               VALUES (?, ?, ?, ?, ?)""",
            (patient_id, appointment_date, doctor_name, reason, notes),
        )
        await db.commit()
        return cursor.lastrowid


async def get_upcoming_appointments(patient_id: str) -> List[Dict[str, Any]]:
    """Get upcoming doctor appointments."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT * FROM doctor_appointments 
            WHERE patient_id = ? AND appointment_date > datetime('now')
            ORDER BY appointment_date ASC
        """
        async with db.execute(query, (patient_id,)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def seed_drug_interactions() -> None:
    """Seed common drug interactions."""
    interactions = [
        ('Warfarin', 'Aspirin', 'high', 'قد يزيد من خطر النزف'),
        ('Warfarin', 'Ibuprofen', 'high', 'قد يزيد من خطر النزف'),
        ('Metformin', 'Alcohol', 'medium', 'قد يسبب حموضة اللاكتيك'),
        ('Statins', 'Grapefruit Juice', 'medium', 'قد يزيد من تركيز الدواء'),
        ('ACE Inhibitors', 'NSAIDs', 'medium', 'قد يؤثر على الكلى'),
        ('Beta Blockers', 'NSAIDs', 'medium', 'قد يقلل من فعالية الدواء'),
    ]
    
    async with connect_db() as db:
        for drug1, drug2, severity, desc in interactions:
            try:
                await db.execute(
                    """INSERT OR IGNORE INTO drug_interactions(drug1, drug2, severity, description) 
                       VALUES (?, ?, ?, ?)""",
                    (drug1, drug2, severity, desc),
                )
            except Exception as e:
                logger.warning(f'Failed to insert interaction {drug1}-{drug2}: {e}')
        
        await db.commit()
        logger.info('Drug interactions seeded')
