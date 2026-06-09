# تحديثات بوت الصيدلية - الميزات الجديدة

## 📌 الملخص
هذا الملف يحتوي على الدوال المحدثة والجديدة لتحقيق الميزات الثلاث:
1. التسجيل التلقائي (Auto-Save)
2. عرض ومراقبة مواعيد الجرعات (Intake Logs Monitor)
3. التحكم الكامل بدون ID

---

## 🗄️ **database_v2.py** - إضافة دالة واحدة

أضف هذه الدالة في نهاية الملف:

```python
async def get_recent_intake_logs_for_medicine(medicine_id: int, limit: int = 5) -> List[Dict[str, Any]]:
    """جلب آخر سجلات الجرعات لدواء معين (آخر 5 بشكل افتراضي)"""
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
```

---

## 🤖 **main.py** - التحديثات الرئيسية

### 1️⃣ **تحديث دالة `start()` - التسجيل التلقائي**

ابحث عن دالة `start` واستبدلها بهذا:

```python
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await db.init_db()
    user = update.effective_user
    if user:
        # قراءة البيانات مباشرة من التليجرام
        first_name = user.first_name or ''
        last_name = user.last_name or ''
        full_name = f"{first_name} {last_name}".strip() or "مستخدم جديد"
        username = user.username or f"user_{user.id}"
        
        # حفظ تلقائي في قاعدة البيانات
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

---

### 2️⃣ **إضافة دالة جديدة: `format_intake_log_entry()` - تنسيق سجل الجرعة**

أضف هذه الدالة قبل دالة `display_patient_medicines`:

```python
def format_intake_log_entry(log: Dict[str, Any]) -> str:
    """تنسيق سجل الجرعة لعرضه بشكل جميل"""
    status = log.get('status', 'unknown')
    timestamp = log.get('timestamp', 'غير معروف')
    
    # تحويل الحالة إلى عربي
    status_ar = {
        'taken': '✅ تم التناول',
        'skipped': '❌ تم التخطي',
        'missed': '⚠️ تم التفويت'
    }.get(status, f'📌 {status}')
    
    return f"  {status_ar} • {timestamp}"
```

---

### 3️⃣ **تحديث دالة `display_patient_medicines()` - عرض الجرعات المسجلة**

ابحث عن دالة `display_patient_medicines` واستبدلها بهذا:

```python
async def display_patient_medicines(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    patient_id: str,
    admin_view: bool = False,
    show_refill_buttons: bool = False,
) -> None:
    try:
        medicines = await db.get_patient_medicines(patient_id)
        if not medicines:
            await safe_reply(update, 'لا توجد أدوية مسجلة في هذا الحساب.', reply_markup=build_main_menu_markup(patient_id))
            return

        lines: List[str] = []
        keyboard_rows: List[List[InlineKeyboardButton]] = []
        
        for med in medicines:
            lines.append(f'\n💊 {med["name"]} | {med["dosage"]} | 📦 {med["stock_quantity"]}')
            
            # جلب آخر سجلات الجرعات لهذا الدواء
            intake_logs = await db.get_recent_intake_logs_for_medicine(med["id"], limit=3)
            if intake_logs:
                lines.append('  📋 آخر الجرعات:')
                for log in intake_logs:
                    lines.append(format_intake_log_entry(log))
            else:
                lines.append('  📋 لا توجد سجلات جرعات بعد')
            
            # أزرار التحكم - تعتمد كلياً على callback_data
            row: List[InlineKeyboardButton] = []
            if admin_view:
                row.append(InlineKeyboardButton('➕ 30 حبة', callback_data=f'refill_{med["id"]}'))
                row.append(InlineKeyboardButton('🗑️ حذف', callback_data=f'delete_{med["id"]}'))
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

---

### 4️⃣ **تحديث دالة `show_pharmacist_dashboard()` - قائمة المرضى بدون ID**

ابحث عن دالة `show_pharmacist_dashboard` واستبدلها بهذا:

```python
async def show_pharmacist_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        # جلب كل المرضى الذين لديهم أدوية مسجلة
        patients = await db.get_all_patients_with_medicines()
        
        if not patients:
            await safe_reply(
                update,
                '📭 لا يوجد مرضى مسجلين حالياً.',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton('📢 إذاعة رسالة', callback_data='pharmacist_broadcast')]
                ])
            )
            return
        
        # بناء قائمة الأزرار بأسماء المرضى (الـ callback_data يحتوي على patient_id)
        keyboard_rows: List[List[InlineKeyboardButton]] = []
        for patient in patients:
            patient_id = patient['user_id']
            patient_name = patient.get('full_name', 'مريض')
            keyboard_rows.append([
                InlineKeyboardButton(f'👤 {patient_name}', callback_data=f'patient_view_{patient_id}')
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
            '👨‍⚕️ لوحة الصيدلي\n\nاختر مريضاً لعرض أدويته:',
            reply_markup=InlineKeyboardMarkup(keyboard_rows),
        )
    except Exception as e:
        logger.error(f"CRITICAL ERROR in show_pharmacist_dashboard: {e}", exc_info=True)
        await safe_reply(update, 'حدث خطأ في لوحة الصيدلي. حاول مرة أخرى لاحقًا.')
```

---

### 5️⃣ **إضافة دالة مساعدة: `get_all_patients_with_medicines()` في database_v2.py**

أضف هذه الدالة في database_v2.py:

```python
async def get_all_patients_with_medicines() -> List[Dict[str, Any]]:
    """جلب جميع المرضى الذين لديهم أدوية مسجلة"""
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
```

---

### 6️⃣ **تحديث دالة `handle_callback_query()` - إضافة معالج جديد**

في دالة `handle_callback_query`، أضف هذا الكود الجديد بعد السطر `await query.answer()`:

```python
# ===== معالج جديد: عرض أدوية المريض من قائمة الصيدلي =====
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

## 🔄 **خطوات التطبيق:**

1. **في database_v2.py:**
   - أضف الدالتين الجديدتين في النهاية:
     - `get_recent_intake_logs_for_medicine()`
     - `get_all_patients_with_medicines()`

2. **في main.py:**
   - استبدل دالة `start()`
   - استبدل دالة `display_patient_medicines()`
   - استبدل دالة `show_pharmacist_dashboard()`
   - أضف الدالة `format_intake_log_entry()`
   - عدّل دالة `handle_callback_query()` بإضافة المعالج الجديد `patient_view_`

---

## ✅ **الميزات المحققة:**

| الميزة | الحالة | التفاصيل |
|--------|--------|---------|
| ✔️ التسجيل التلقائي | مكتملة | يقرأ first_name و username تلقائياً من التليجرام |
| ✔️ عرض سجل الجرعات | مكتملة | يعرض آخر 3 جرعات مع حالتها والتاريخ |
| ✔️ بدون ID يدوي | مكتملة | جميع الأزرار تستخدم callback_data فقط |
| ✔️ قائمة مرضى ديناميكية | مكتملة | تعرض فقط المرضى الذين لديهم أدوية |
