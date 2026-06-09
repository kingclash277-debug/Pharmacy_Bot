# 🔧 مرجع سريع - قطع الكود المحدثة

## 📦 database_v2.py - الإضافات فقط

**أضف هذه الدالتين في النهاية:**

```python
async def get_recent_intake_logs_for_medicine(medicine_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """جلب آخر سجلات الجرعات لدواء معين (آخر 5 بشكل افتراضي)."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
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
        async with aiosqlite.connect(DB_PATH) as db:
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
```

---

## 🤖 main.py - التحديثات الأساسية

### 1️⃣ دالة `start()` - استبدل بالكامل:

```python
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """دالة البداية - التسجيل التلقائي للمستخدمين."""
    await db.init_db()
    user = update.effective_user
    if user:
        # قراءة البيانات مباشرة من التليجرام وحفظها تلقائياً
        first_name = user.first_name or ''
        last_name = user.last_name or ''
        full_name = f"{first_name} {last_name}".strip() or "مستخدم جديد"
        username = user.username or f"user_{user.id}"
        
        # حفظ تلقائي في قاعدة البيانات كـ patient أساساً
        role = 'admin' if str(user.id) == str(ADMIN_ID) else 'patient'
        await db.add_user(
            user_id=str(user.id),
            username=username,
            full_name=full_name,
            role=role,
        )
        await db.update_user_state(str(user.id), IDLE)
        await db.clear_user_pending_data(str(user.id))
    await send_main_menu(update, context)
```

### 2️⃣ دالة `display_patient_medicines()` - استبدل بالكامل:

```python
async def display_patient_medicines(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    patient_id: str,
    admin_view: bool = False,
    show_refill_buttons: bool = False,
) -> None:
    """عرض أدوية المريض مع سجل آخر الجرعات - بدون الحاجة لـ ID يدوي."""
    try:
        medicines = await db.get_patient_medicines(patient_id)
        if not medicines:
            await safe_reply(update, 'لا توجد أدوية مسجلة في هذا الحساب.', reply_markup=build_main_menu_markup(patient_id))
            return

        lines: List[str] = []
        keyboard_rows: List[List[InlineKeyboardButton]] = []
        
        for med in medicines:
            lines.append(f'\n💊 **{med["name"]}** | {med["dosage"]} | 📦 المخزون: {med["stock_quantity"]}')
            
            # جلب آخر سجلات الجرعات لهذا الدواء
            intake_logs = await db.get_recent_intake_logs_for_medicine(med["id"], limit=3)
            if intake_logs:
                lines.append('  📋 آخر الجرعات:')
                for log in intake_logs:
                    status = log.get('status', 'unknown')
                    timestamp = log.get('timestamp', 'غير معروف')
                    
                    # تحويل الحالة إلى عربي
                    status_ar = {
                        'taken': '✅ تم التناول',
                        'skipped': '❌ تم التخطي',
                        'missed': '⚠️ تم التفويت'
                    }.get(status, f'📌 {status}')
                    
                    lines.append(f'    {status_ar} • {timestamp}')
            else:
                lines.append('    📋 لا توجد سجلات جرعات بعد')
            
            # أزرار التحكم - تعتمد كلياً على callback_data بدون الحاجة لإدخال ID
            row: List[InlineKeyboardButton] = []
            if admin_view:
                row.append(InlineKeyboardButton('➕ إضافة 30 حبة', callback_data=f'refill_{med["id"]}'))
                row.append(InlineKeyboardButton('🗑️ حذف الدواء', callback_data=f'delete_{med["id"]}'))
            if row:
                keyboard_rows.append(row)

        prefix = '📋 أدويتي:' if not admin_view else '📋 أدوية المريض:'
        await safe_reply(
            update,
            f'{prefix}' + ''.join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None,
        )
    except Exception as e:
        logger.error(f"CRITICAL ERROR in display_patient_medicines: {e}", exc_info=True)
        await safe_reply(update, 'حدث خطأ أثناء عرض الأدوية. حاول مرة أخرى لاحقًا.')
```

### 3️⃣ دالة `show_pharmacist_dashboard()` - استبدل بالكامل:

```python
async def show_pharmacist_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """لوحة الصيدلي - عرض قائمة المرضى بدون الحاجة لإدخال ID يدوي."""
    try:
        # جلب جميع المرضى الذين لديهم أدوية مسجلة - ديناميكياً وبدون ID يدوي
        patients = await db.get_all_patients_with_medicines()
        
        if not patients:
            await safe_reply(
                update,
                '📭 لا يوجد مرضى مسجلين حالياً.',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('📢 إذاعة رسالة', callback_data='pharmacist_broadcast')],
                    [InlineKeyboardButton('🔙 العودة', callback_data='back_to_main')]
                ])
            )
            return
        
        # بناء قائمة الأزرار بأسماء المرضى فقط - callback_data يحتوي على patient_id
        keyboard_rows: List[List[InlineKeyboardButton]] = []
        for patient in patients:
            patient_id = patient['user_id']
            patient_name = patient.get('full_name', 'مريض')
            username_display = f" (@{patient['username']})" if patient.get('username') else ""
            
            keyboard_rows.append([
                InlineKeyboardButton(f'👤 {patient_name}{username_display}', callback_data=f'patient_view_{patient_id}')
            ])
        
        # إضافة أزرار الإجراءات الإدارية في الأسفل
        keyboard_rows.append([
            InlineKeyboardButton('📦 إضافة مخزون سريع', callback_data='pharmacist_refill')
        ])
        keyboard_rows.append([
            InlineKeyboardButton('📢 إذاعة رسالة جماعية', callback_data='pharmacist_broadcast')
        ])
        
        await safe_reply(
            update,
            f'👨‍⚕️ لوحة الصيدلي\n\n📊 عدد المرضى: {len(patients)}\n\nاختر مريضاً لعرض أدويته وإدارة جرعاته:',
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )
    except Exception as e:
        logger.error(f"CRITICAL ERROR in show_pharmacist_dashboard: {e}", exc_info=True)
        await safe_reply(update, 'حدث خطأ في لوحة الصيدلي. حاول مرة أخرى لاحقًا.')
```

### 4️⃣ في دالة `handle_callback_query()` - أضف هذا بعد `await query.answer()` مباشرة:

```python
# ===== معالج جديد: عرض أدوية المريض من قائمة الصيدلي (بدون ID يدوي) =====
if query.data.startswith('patient_view_'):
    if not is_admin:
        await query.edit_message_text('عذراً، هذه الخاصية محجوزة للصيدلي فقط.')
        return
    
    patient_id = query.data.split('_', 2)[2]
    patient = await db.get_user(patient_id)
    if not patient:
        await query.edit_message_text('لم يتم العثور على المريض.')
        return
    
    # حذف الرسالة القديمة وإرسال رسالة جديدة بأدوية المريض
    try:
        await query.delete_message()
    except:
        pass
    
    await display_patient_medicines(update, context, patient_id, admin_view=True, show_refill_buttons=True)
    return
```

---

## ✅ قائمة التحقق

- [ ] إضافة الدالتين الجديدتين في `database_v2.py`
- [ ] تحديث دالة `start()` في `main.py`
- [ ] تحديث دالة `display_patient_medicines()` في `main.py`
- [ ] تحديث دالة `show_pharmacist_dashboard()` في `main.py`
- [ ] إضافة معالج `patient_view_` في `handle_callback_query()`
- [ ] اختبار الميزات الثلاث
- [ ] تشغيل البوت والتحقق من عدم وجود أخطاء

---

**جاهز للاستخدام! 🎉**
