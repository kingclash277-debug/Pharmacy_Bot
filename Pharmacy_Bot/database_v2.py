import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

import aiosqlite

logger = logging.getLogger(__name__)
DB_PATH = Path(__file__).with_name("pharmacy_bot.db")
DB_TIMEOUT = 30

@asynccontextmanager
async def connect_db():
    conn = await aiosqlite.connect(DB_PATH, timeout=DB_TIMEOUT)
    await conn.execute("PRAGMA foreign_keys = ON;")
    await conn.execute("PRAGMA journal_mode = WAL;")
    await conn.execute("PRAGMA busy_timeout = 30000;")
    try:
        yield conn
    finally:
        await conn.close()


async def init_db() -> None:
    """Initialize the database and create tables if they do not exist."""
    try:
        async with connect_db() as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    role TEXT NOT NULL DEFAULT 'patient',
                    language TEXT NOT NULL DEFAULT 'ar',
                    current_state TEXT NOT NULL DEFAULT 'IDLE',
                    pending_med_name TEXT DEFAULT '',
                    pending_med_dosage TEXT DEFAULT '',
                    pending_med_stock INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS medicines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    dosage TEXT NOT NULL,
                    stock_quantity INTEGER NOT NULL DEFAULT 0,
                    refill_threshold INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(patient_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    medicine_id INTEGER NOT NULL,
                    reminder_time TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(medicine_id) REFERENCES medicines(id) ON DELETE CASCADE
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS intake_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    medicine_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(medicine_id) REFERENCES medicines(id) ON DELETE CASCADE
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS refill_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    medicine_id INTEGER NOT NULL,
                    patient_id TEXT NOT NULL,
                    note TEXT DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(medicine_id) REFERENCES medicines(id) ON DELETE CASCADE,
                    FOREIGN KEY(patient_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS health_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    reading_type TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT,
                    notes TEXT,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(patient_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS drug_interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drug1 TEXT NOT NULL,
                    drug2 TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    description TEXT,
                    UNIQUE(drug1, drug2)
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chronic_diseases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    disease_name TEXT NOT NULL,
                    diagnosis_date DATE,
                    notes TEXT,
                    FOREIGN KEY(patient_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS doctor_appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    patient_id TEXT NOT NULL,
                    appointment_date DATETIME NOT NULL,
                    doctor_name TEXT,
                    reason TEXT,
                    notes TEXT,
                    reminder_sent INTEGER DEFAULT 0,
                    FOREIGN KEY(patient_id) REFERENCES users(user_id) ON DELETE CASCADE
                );
                """
            )
            await db.commit()

            async with db.execute("PRAGMA table_info(users)") as cursor:
                rows = await cursor.fetchall()
                existing_columns = {row[1] for row in rows}

            if 'current_state' not in existing_columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN current_state TEXT NOT NULL DEFAULT 'IDLE'"
                )
            if 'pending_med_name' not in existing_columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN pending_med_name TEXT DEFAULT ''"
                )
            if 'pending_med_dosage' not in existing_columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN pending_med_dosage TEXT DEFAULT ''"
                )
            if 'pending_med_stock' not in existing_columns:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN pending_med_stock INTEGER NOT NULL DEFAULT 0"
                )
            await db.commit()
            logger.info("Database initialized at %s", DB_PATH)
    except Exception:
        logger.exception("Failed to initialize database")
        raise


def _row_to_dict(row: aiosqlite.Row) -> Dict[str, Any]:
    return {key: row[key] for key in row.keys()}


async def get_user_state(user_id: str) -> str:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT current_state FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 'IDLE'


async def update_user_state(user_id: str, new_state: str) -> bool:
    async with connect_db() as db:
        result = await db.execute(
            "UPDATE users SET current_state = ? WHERE user_id = ?",
            (new_state, user_id),
        )
        await db.commit()
        return result.rowcount > 0


async def set_user_pending_data(
    user_id: str,
    pending_med_name: Optional[str] = None,
    pending_med_dosage: Optional[str] = None,
    pending_med_stock: Optional[int] = None,
) -> None:
    columns = []
    values: List[Any] = []
    if pending_med_name is not None:
        columns.append('pending_med_name = ?')
        values.append(pending_med_name)
    if pending_med_dosage is not None:
        columns.append('pending_med_dosage = ?')
        values.append(pending_med_dosage)
    if pending_med_stock is not None:
        columns.append('pending_med_stock = ?')
        values.append(pending_med_stock)
    if not columns:
        return
    values.append(user_id)
    async with connect_db() as db:
        await db.execute(
            f"UPDATE users SET {', '.join(columns)} WHERE user_id = ?",
            tuple(values),
        )
        await db.commit()


async def clear_user_pending_data(user_id: str) -> bool:
    async with connect_db() as db:
        result = await db.execute(
            "UPDATE users SET pending_med_name = '', pending_med_dosage = '', pending_med_stock = 0 WHERE user_id = ?",
            (user_id,),
        )
        await db.commit()
        return result.rowcount > 0


async def get_user_pending_data(user_id: str) -> Dict[str, Any]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT pending_med_name, pending_med_dosage, pending_med_stock FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else {}


async def add_user(
    user_id: str,
    username: str,
    full_name: str,
    role: str = "patient",
    language: str = "ar",
    current_state: str = "IDLE",
    pending_med_name: str = "",
    pending_med_dosage: str = "",
    pending_med_stock: int = 0,
) -> None:
    """Insert or update a user record without resetting existing state."""
    try:
        async with connect_db() as db:
            await db.execute(
                """
                INSERT INTO users(user_id, username, full_name, role, language, current_state, pending_med_name, pending_med_dosage, pending_med_stock)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    full_name = excluded.full_name,
                    role = excluded.role,
                    language = excluded.language;
                """,
                (
                    user_id,
                    username,
                    full_name,
                    role,
                    language,
                    current_state,
                    pending_med_name,
                    pending_med_dosage,
                    pending_med_stock,
                ),
            )
            await db.commit()
            logger.debug("Added/updated user %s (%s)", user_id, username)
    except Exception:
        logger.exception("Failed to add or update user %s", user_id)
        raise


async def add_medicine(
    patient_id: str,
    name: str,
    dosage: str,
    stock_quantity: int,
    refill_threshold: int = 1,
) -> int:
    """Insert a new medicine record."""
    try:
        async with connect_db() as db:
            cursor = await db.execute(
                """
                INSERT INTO medicines(patient_id, name, dosage, stock_quantity, refill_threshold)
                VALUES (?, ?, ?, ?, ?)
                """,
                (patient_id, name, dosage, stock_quantity, refill_threshold),
            )
            await db.commit()
            return cursor.lastrowid
    except Exception:
        logger.exception("Failed to add medicine for patient %s", patient_id)
        raise


async def get_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a user by user_id."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None


async def get_all_active_reminders() -> List[Dict[str, Any]]:
    """Return all active reminders joined with their medicine and patient data."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT
                r.id AS reminder_id,
                r.medicine_id,
                r.reminder_time,
                r.is_active,
                m.patient_id,
                m.name AS medicine_name,
                m.dosage,
                m.stock_quantity,
                m.refill_threshold,
                u.username,
                u.full_name,
                u.language
            FROM reminders r
            JOIN medicines m ON m.id = r.medicine_id
            JOIN users u ON u.user_id = m.patient_id
            WHERE r.is_active = 1;
        """
        async with db.execute(query) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def get_patient_medicines(patient_id: str) -> List[Dict[str, Any]]:
    """Return medicines belonging to a single patient."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, name, dosage, stock_quantity FROM medicines WHERE patient_id = ? ORDER BY id DESC",
            (patient_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def get_patient_profile(patient_id: str) -> Optional[Dict[str, Any]]:
    """Return a clinical profile for a patient, with medications and reminder times."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (patient_id,)) as cursor:
            user_row = await cursor.fetchone()
            if not user_row:
                return None
            user = _row_to_dict(user_row)

        query = """
            SELECT
                m.id AS medicine_id,
                m.name,
                m.dosage,
                m.stock_quantity,
                m.refill_threshold,
                IFNULL(r.reminder_time, 'غير محدد') AS reminder_time
            FROM medicines m
            LEFT JOIN reminders r ON r.medicine_id = m.id AND r.is_active = 1
            WHERE m.patient_id = ?
            ORDER BY m.id DESC
        """
        async with db.execute(query, (patient_id,)) as cursor:
            rows = await cursor.fetchall()
            medicines = [_row_to_dict(row) for row in rows]

    return {"user": user, "medicines": medicines}


async def get_medicine_by_id(med_id: int) -> Optional[Dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM medicines WHERE id = ?", (med_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None


async def get_reminders_for_medicine(medicine_id: int) -> List[Dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id AS reminder_id, reminder_time, is_active FROM reminders WHERE medicine_id = ? AND is_active = 1 ORDER BY reminder_time",
            (medicine_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def add_refill_request(medicine_id: int, patient_id: str, note: str = "") -> int:
    async with connect_db() as db:
        cursor = await db.execute(
            "INSERT INTO refill_requests(medicine_id, patient_id, note) VALUES (?, ?, ?)",
            (medicine_id, patient_id, note),
        )
        await db.commit()
        return cursor.lastrowid


async def get_refill_requests(patient_id: Optional[str] = None) -> List[Dict[str, Any]]:
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        if patient_id:
            query = """
                SELECT rr.*, m.name AS medicine_name, u.full_name, u.username
                FROM refill_requests rr
                JOIN medicines m ON m.id = rr.medicine_id
                JOIN users u ON u.user_id = rr.patient_id
                WHERE rr.patient_id = ?
                ORDER BY rr.created_at DESC
            """
            params = (patient_id,)
        else:
            query = """
                SELECT rr.*, m.name AS medicine_name, u.full_name, u.username
                FROM refill_requests rr
                JOIN medicines m ON m.id = rr.medicine_id
                JOIN users u ON u.user_id = rr.patient_id
                ORDER BY rr.created_at DESC
            """
            params = ()
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def delete_medicine(med_id: int) -> bool:
    """Delete a medicine and its associated reminders."""
    try:
        async with connect_db() as db:
            result = await db.execute("DELETE FROM medicines WHERE id = ?", (med_id,))
            await db.commit()
            deleted = result.rowcount > 0
            logger.debug("Deleted medicine %s: %s", med_id, deleted)
            return deleted
    except Exception:
        logger.exception("Failed to delete medicine %s", med_id)
        raise


async def update_reminder_time(med_id: int, new_time: str) -> bool:
    """Update the reminder time for a medicine's reminder entry."""
    try:
        async with connect_db() as db:
            result = await db.execute(
                "UPDATE reminders SET reminder_time = ? WHERE medicine_id = ?",
                (new_time, med_id),
            )
            await db.commit()
            updated = result.rowcount > 0
            logger.debug("Updated reminder time for medicine %s to %s", med_id, new_time)
            return updated
    except Exception:
        logger.exception("Failed to update reminder time for medicine %s", med_id)
        raise


async def delete_reminders_for_medicine(medicine_id: int) -> None:
    """Delete all reminder entries for a specific medicine."""
    async with connect_db() as db:
        await db.execute("DELETE FROM reminders WHERE medicine_id = ?", (medicine_id,))
        await db.commit()


async def add_reminder(medicine_id: int, reminder_time: str) -> int:
    """Create a reminder for a medicine."""
    async with connect_db() as db:
        cursor = await db.execute(
            "INSERT INTO reminders(medicine_id, reminder_time) VALUES (?, ?)",
            (medicine_id, reminder_time),
        )
        await db.commit()
        return cursor.lastrowid


async def log_intake(medicine_id: int, status: str) -> None:
    """Record a medicine intake status."""
    async with connect_db() as db:
        await db.execute(
            "INSERT INTO intake_logs(medicine_id, status) VALUES (?, ?)",
            (medicine_id, status),
        )
        await db.commit()


async def decrement_stock(medicine_id: int, amount: int = 1) -> bool:
    """Decrease stock quantity when the patient confirms intake."""
    async with connect_db() as db:
        await db.execute(
            "UPDATE medicines SET stock_quantity = MAX(stock_quantity - ?, 0) WHERE id = ?",
            (amount, medicine_id),
        )
        await db.commit()
        return True


async def get_low_stock_items(threshold: int = 5) -> List[Dict[str, Any]]:
    """Return medicines with stock below threshold."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM medicines WHERE stock_quantity <= ? ORDER BY stock_quantity ASC",
            (threshold,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def get_intake_logs_for_patient(patient_id: str) -> List[Dict[str, Any]]:
    """Return intake logs for a patient's medicines."""
    async with connect_db() as db:
        db.row_factory = aiosqlite.Row
        query = """
            SELECT l.id, l.medicine_id, l.status, l.timestamp, m.name AS medicine_name
            FROM intake_logs l
            JOIN medicines m ON m.id = l.medicine_id
            WHERE m.patient_id = ?
            ORDER BY l.timestamp DESC
        """
        async with db.execute(query, (patient_id,)) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def get_recent_intake_logs_for_medicine(medicine_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """جلب آخر سجلات الجرعات لدواء معين (آخر 5 بشكل افتراضي)."""
    try:
        async with connect_db() as db:
            db.row_factory = aiosqlite.Row
            query = """
                SELECT id, medicine_id, status, timestamp
                FROM intake_logs
                WHERE medicine_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """
            async with db.execute(query, (medicine_id, limit)) as cursor:
                rows = await cursor.fetchall()
                return [_row_to_dict(row) for row in rows]
    except Exception:
        logger.exception("Failed to get intake logs for medicine %s", medicine_id)
        return []


async def get_all_patients_with_medicines() -> List[Dict[str, Any]]:
    """جلب جميع المرضى الذين لديهم أدوية مسجلة."""
    try:
        async with connect_db() as db:
            db.row_factory = aiosqlite.Row
            query = """
                SELECT DISTINCT u.user_id, u.full_name, u.username
                FROM users u
                JOIN medicines m ON u.user_id = m.patient_id
                WHERE u.role = 'patient'
                ORDER BY u.full_name ASC
            """
            async with db.execute(query) as cursor:
                rows = await cursor.fetchall()
                return [_row_to_dict(row) for row in rows]
    except Exception:
        logger.exception("Failed to get patients with medicines")
        return []


async def refill_medicine(med_id: int, multiplier: int = 2) -> bool:
    """Refill a medicine based on its refill threshold."""
    try:
        async with connect_db() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT refill_threshold FROM medicines WHERE id = ?", (med_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return False
                threshold = row["refill_threshold"]
                refill_amount = max(threshold * multiplier, 10)
            await db.execute(
                "UPDATE medicines SET stock_quantity = ? WHERE id = ?",
                (refill_amount, med_id),
            )
            await db.commit()
            logger.debug("Refilled medicine %s to %s units", med_id, refill_amount)
            return True
    except Exception:
        logger.exception("Failed to refill medicine %s", med_id)
        raise


async def update_medicine_dosage(med_id: int, new_dosage: str) -> bool:
    """Update the dosage text for a medicine."""
    try:
        async with connect_db() as db:
            result = await db.execute(
                "UPDATE medicines SET dosage = ? WHERE id = ?",
                (new_dosage, med_id),
            )
            await db.commit()
            return result.rowcount > 0
    except Exception:
        logger.exception("Failed to update dosage for medicine %s", med_id)
        return False


async def update_medicine_stock(med_id: int, new_stock: int) -> bool:
    """Set the stock quantity for a medicine to a specific number."""
    try:
        async with connect_db() as db:
            result = await db.execute(
                "UPDATE medicines SET stock_quantity = ? WHERE id = ?",
                (new_stock, med_id),
            )
            await db.commit()
            return result.rowcount > 0
    except Exception:
        logger.exception("Failed to update stock for medicine %s", med_id)
        return False


# ===== Health Tracking & Compliance =====

async def get_compliance_rate(patient_id: str, days: int = 7) -> Dict[str, Any]:
    """Calculate medicine adherence rate (taken/total) for the past N days."""
    try:
        async with connect_db() as db:
            query = f"""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'taken' THEN 1 ELSE 0 END) as taken,
                    SUM(CASE WHEN status = 'skipped' THEN 1 ELSE 0 END) as skipped,
                    SUM(CASE WHEN status = 'missed' THEN 1 ELSE 0 END) as missed
                FROM intake_logs
                WHERE medicine_id IN (SELECT id FROM medicines WHERE patient_id = ?)
                  AND datetime(timestamp) >= datetime('now', '-{days} days')
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
            query = "SELECT DISTINCT name FROM medicines WHERE patient_id = ?"
            async with db.execute(query, (patient_id,)) as cursor:
                med_rows = await cursor.fetchall()
            
            patient_meds = [m['name'] for m in med_rows]
            if len(patient_meds) < 2:
                return []
            
            interactions = []
            for i, drug1 in enumerate(patient_meds):
                for drug2 in patient_meds[i+1:]:
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
    """Get health readings for a patient."""
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
