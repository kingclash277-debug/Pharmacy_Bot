#!/usr/bin/env python3
# Quick check for new database functions
import sys
sys.path.insert(0, r'c:\Users\2026\Documents\Pharmacy_Bot')

try:
    import database_v2 as db
    print("Checking database_v2 module...")
    
    # Check for new functions
    functions = [
        'get_compliance_rate',
        'check_drug_interactions',
        'add_health_reading',
        'get_health_readings',
        'add_chronic_disease',
        'get_patient_diseases',
        'add_doctor_appointment',
        'get_upcoming_appointments',
        'seed_drug_interactions'
    ]
    
    for func_name in functions:
        if hasattr(db, func_name):
            print(f"✅ {func_name} found")
        else:
            print(f"❌ {func_name} NOT found")
            
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
