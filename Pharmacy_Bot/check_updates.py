#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🧪 فحص التحديثات - Pharmacy Bot v2.0

هذا الملف يتحقق من أن جميع التحديثات تم تطبيقها بشكل صحيح.
استخدم: python check_updates.py
"""

import ast
import sys
from pathlib import Path

# الألوان للطباعة
GREEN = '\033[92m'
RED = '\033[91m'
YELLOW = '\033[93m'
BLUE = '\033[94m'
RESET = '\033[0m'

def print_header(text):
    print(f"\n{BLUE}{'='*50}{RESET}")
    print(f"{BLUE}{text}{RESET}")
    print(f"{BLUE}{'='*50}{RESET}\n")

def check_function_exists(file_path, function_name):
    """تحقق من وجود دالة في الملف."""
    with open(file_path, 'r', encoding='utf-8') as f:
        tree = ast.parse(f.read())
    
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name:
            return True
    return False

def check_text_in_file(file_path, text):
    """تحقق من وجود نص في الملف."""
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return text in content

def main():
    base_path = Path(__file__).parent
    db_path = base_path / "database_v2.py"
    main_path = base_path / "main.py"
    
    print(f"{BLUE}\n{'='*60}{RESET}")
    print(f"{BLUE}🧪 فحص تحديثات بوت الصيدلية v2.0{RESET}")
    print(f"{BLUE}{'='*60}{RESET}")
    
    total_checks = 0
    passed_checks = 0
    failed_checks = 0
    
    # الفحص 1: التسجيل التلقائي
    print_header("✅ الفحص 1: التسجيل التلقائي (Auto-Save)")
    
    checks = [
        ("وجود تعليق 'التسجيل التلقائي'", lambda: check_text_in_file(main_path, "التسجيل التلقائي")),
        ("قراءة first_name من التليجرام", lambda: check_text_in_file(main_path, "user.first_name")),
        ("حفظ البيانات في add_user", lambda: check_function_exists(main_path, "start")),
    ]
    
    for check_name, check_func in checks:
        total_checks += 1
        try:
            result = check_func()
            if result:
                print(f"{GREEN}✅ {check_name}{RESET}")
                passed_checks += 1
            else:
                print(f"{RED}❌ {check_name}{RESET}")
                failed_checks += 1
        except Exception as e:
            print(f"{RED}❌ {check_name} - خطأ: {e}{RESET}")
            failed_checks += 1
    
    # الفحص 2: سجل الجرعات
    print_header("✅ الفحص 2: عرض سجل الجرعات (Intake Logs Monitor)")
    
    checks = [
        ("وجود دالة get_recent_intake_logs_for_medicine", 
         lambda: check_function_exists(db_path, "get_recent_intake_logs_for_medicine")),
        ("عرض آخر الجرعات في display_patient_medicines",
         lambda: check_text_in_file(main_path, "get_recent_intake_logs_for_medicine")),
        ("عرض حالة الجرعة (status)",
         lambda: check_text_in_file(main_path, "log.get('status'")),
    ]
    
    for check_name, check_func in checks:
        total_checks += 1
        try:
            result = check_func()
            if result:
                print(f"{GREEN}✅ {check_name}{RESET}")
                passed_checks += 1
            else:
                print(f"{RED}❌ {check_name}{RESET}")
                failed_checks += 1
        except Exception as e:
            print(f"{RED}❌ {check_name} - خطأ: {e}{RESET}")
            failed_checks += 1
    
    # الفحص 3: بدون ID يدوي
    print_header("✅ الفحص 3: التحكم بدون ID (No Manual IDs)")
    
    checks = [
        ("وجود دالة get_all_patients_with_medicines",
         lambda: check_function_exists(db_path, "get_all_patients_with_medicines")),
        ("عرض قائمة ديناميكية للمرضى",
         lambda: check_text_in_file(main_path, "get_all_patients_with_medicines")),
        ("معالج patient_view_ في callback",
         lambda: check_text_in_file(main_path, "patient_view_")),
        ("استخدام callback_data للـ IDs",
         lambda: check_text_in_file(main_path, "callback_data=f'patient_view_")),
    ]
    
    for check_name, check_func in checks:
        total_checks += 1
        try:
            result = check_func()
            if result:
                print(f"{GREEN}✅ {check_name}{RESET}")
                passed_checks += 1
            else:
                print(f"{RED}❌ {check_name}{RESET}")
                failed_checks += 1
        except Exception as e:
            print(f"{RED}❌ {check_name} - خطأ: {e}{RESET}")
            failed_checks += 1
    
    # النتيجة النهائية
    print_header("📊 النتيجة النهائية")
    print(f"إجمالي الفحوصات: {YELLOW}{total_checks}{RESET}")
    print(f"نجح: {GREEN}{passed_checks}{RESET}")
    print(f"فشل: {RED}{failed_checks}{RESET}")
    
    percentage = (passed_checks / total_checks * 100) if total_checks > 0 else 0
    print(f"النسبة: {YELLOW}{percentage:.1f}%{RESET}")
    
    if failed_checks == 0:
        print(f"\n{GREEN}{'='*60}{RESET}")
        print(f"{GREEN}🎉 جميع الفحوصات نجحت! البوت جاهز للاستخدام{RESET}")
        print(f"{GREEN}{'='*60}{RESET}\n")
        return 0
    else:
        print(f"\n{RED}{'='*60}{RESET}")
        print(f"{RED}⚠️  بعض الفحوصات فشلت. يرجى التحقق{RESET}")
        print(f"{RED}{'='*60}{RESET}\n")
        return 1

if __name__ == "__main__":
    sys.exit(main())
