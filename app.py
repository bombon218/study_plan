import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import plotly.express as px
from streamlit_calendar import calendar
import psycopg2
import os

# --- データベース接続設定 (クラウド対応) ---
def get_db_connection():
    # Streamlit CloudのSecrets（またはローカルの.streamlit/secrets.toml）から接続URLを取得
    try:
        return psycopg2.connect(st.secrets["DB_URL"])
    except Exception as e:
        st.error(f"データベース接続エラー: 設定（Secrets）を確認してください。\n{e}")
        st.stop()

# --- データベースの初期化 ---
def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    # PostgreSQLでは AUTOINCREMENT ではなく SERIAL を使用
    c.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            subject TEXT,
            topic TEXT,
            next_review DATE,
            interval INTEGER,
            ease_factor REAL,
            is_review INTEGER DEFAULT 0,
            color TEXT DEFAULT '#3498db'
        )
    ''')
    
    c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='tasks'")
    columns = [row[0] for row in c.fetchall()]
    if 'color' not in columns:
        c.execute("ALTER TABLE tasks ADD COLUMN color TEXT DEFAULT '#3498db'")
    if 'is_review' not in columns:
        c.execute("ALTER TABLE tasks ADD COLUMN is_review INTEGER DEFAULT 0")

    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS history (
            id SERIAL PRIMARY KEY,
            task_id INTEGER,
            subject TEXT,
            reviewed_date DATE,
            feedback TEXT
        )
    ''')
    
    c.execute("SELECT count(*) FROM history")
    if c.fetchone()[0] == 0:
        today_str = datetime.now().strftime('%Y-%m-%d')
        # PostgreSQLのプレースホルダーは ? ではなく %s
        c.execute("INSERT INTO history (task_id, subject, reviewed_date, feedback) SELECT id, subject, %s, 'Initial' FROM tasks WHERE is_review = 1", (today_str,))
        
    conn.commit()
    conn.close()

# --- データ操作関数 ---
def update_task(task_id, feedback, current_interval, ease_factor):
    if feedback == 'Again':
        new_interval = 1
        new_ease = max(1.3, ease_factor - 0.2)
    elif feedback == 'Hard':
        new_interval = max(1, int(current_interval * 1.2))
        new_ease = max(1.3, ease_factor - 0.15)
    elif feedback == 'Good':
        new_interval = max(1, int(current_interval * ease_factor))
        new_ease = ease_factor
    elif feedback == 'Easy':
        new_interval = max(1, int(current_interval * ease_factor * 1.3))
        new_ease = ease_factor + 0.15

    today_str = datetime.now().strftime('%Y-%m-%d')
    next_review = (datetime.now() + timedelta(days=new_interval)).strftime('%Y-%m-%d')
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        UPDATE tasks 
        SET next_review = %s, interval = %s, ease_factor = %s, is_review = 1
        WHERE id = %s
    ''', (next_review, new_interval, new_ease, task_id))
    
    c.execute("SELECT subject FROM tasks WHERE id = %s", (task_id,))
    res = c.fetchone()
    if res:
        subject = res[0]
        c.execute("INSERT INTO history (task_id, subject, reviewed_date, feedback) VALUES (%s, %s, %s, %s)",
                  (task_id, subject, today_str, feedback))
                  
    conn.commit()
    conn.close()

def smart_reschedule(target_date, day_weights):
    conn = get_db_connection()
    c = conn.cursor()
    today = datetime.now().date()
    today_str = today.strftime('%Y-%m-%d')

    if sum(day_weights) == 0:
        day_weights = [1] * 7

    # PostgreSQLのUPSERT構文 (ON CONFLICT)
    c.execute('''
        INSERT INTO settings (key, value) VALUES (%s, %s) 
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    ''', ('target_date', target_date.strftime('%Y-%m-%d')))
    
    days_keys = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    for k, w in zip(days_keys, day_weights):
        c.execute('''
            INSERT INTO settings (key, value) VALUES (%s, %s) 
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        ''', (f'weight_{k}', str(w)))

    c.execute(f"SELECT id FROM tasks WHERE is_review = 1 AND next_review < '{today_str}'")
    delayed_reviews = c.fetchall()
    for i, row in enumerate(delayed_reviews):
        offset = i % 3 
        new_date = (today + timedelta(days=offset)).strftime('%Y-%m-%d')
        c.execute("UPDATE tasks SET next_review = %s WHERE id = %s", (new_date, row[0]))

    c.execute("SELECT id FROM tasks WHERE is_review = 0 ORDER BY next_review ASC")
    new_tasks = c.fetchall()

    if new_tasks:
        if (target_date - today).days <= 0:
            target_date = today + timedelta(days=14)

        slots = []
        current_date = today
        while current_date <= target_date:
            weekday = current_date.weekday()
            weight = day_weights[weekday]
            slots.extend([current_date] * weight)
            current_date += timedelta(days=1)
            
        if not slots:
            slots = [today]

        for i, row in enumerate(new_tasks):
            if len(new_tasks) > 1:
                slot_index = int((i / (len(new_tasks) - 1)) * (len(slots) - 1))
            else:
                slot_index = 0
            
            assigned_date = slots[slot_index]
            new_date = assigned_date.strftime('%Y-%m-%d')
            c.execute("UPDATE tasks SET next_review = %s WHERE id = %s", (new_date, row[0]))

    conn.commit()
    conn.close()

def get_default_target_date():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'target_date'")
    res = c.fetchone()
    default_date = None
    if res:
        try:
            default_date = datetime.strptime(res[0], '%Y-%m-%d').date()
        except:
            pass
    if not default_date:
        c.execute("SELECT MAX(next_review) FROM tasks WHERE is_review = 0")
        max_new = c.fetchone()[0]
        if max_new:
            # PostgreSQLではdate型として返ってくるための処理
            if isinstance(max_new, str):
                default_date = datetime.strptime(max_new, '%Y-%m-%d').date()
            else:
                default_date = max_new
    conn.close()
    
    today = datetime.now().date()
    if not default_date or default_date <= today:
        default_date = today + timedelta(days=30)
    return default_date

def get_default_weights():
    conn = get_db_connection()
    c = conn.cursor()
    weights = [1, 1, 1, 1, 1, 3, 3] 
    days_keys = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
    for i, k in enumerate(days_keys):
        c.execute(f"SELECT value FROM settings WHERE key = 'weight_{k}'")
        res = c.fetchone()
        if res:
            try:
                weights[i] = int(res[0])
            except:
                pass
    conn.close()
    return weights

def render_reschedule_alert(key_prefix):
    if 'reschedule_success' in st.session_state and st.session_state.reschedule_success:
        st.success("✅ リスケジュールが完了し、指定した曜日ウェイトで全タスクが再配分されました！")
        st.session_state.reschedule_success = False

    conn = get_db_connection()
    today = datetime.now().date()
    today_str = today.strftime('%Y-%m-%d')

    delayed_df = pd.read_sql_query(f"SELECT next_review FROM tasks WHERE next_review < '{today_str}'", conn)
    new_tasks_count_df = pd.read_sql_query(f"SELECT count(*) FROM tasks WHERE is_review = 0", conn)
    new_tasks_count = new_tasks_count_df.iloc[0,0]
    conn.close()

    default_target = get_default_target_date()
    dw = get_default_weights()

    if not delayed_df.empty:
        delayed_df['next_review'] = pd.to_datetime(delayed_df['next_review']).dt.date
        max_delay_days = (today - delayed_df['next_review'].min()).days
        delayed_count = len(delayed_df)
        
        st.write("▼ 新規タスクの再配分設定")
        c_date, c_weights = st.columns([1, 2])
        with c_date:
            new_target = st.date_input("次の目標日", value=default_target, key=f"resch_date_{key_prefix}")
        with c_weights:
            st.write("曜日ウェイト")
            wc = st.columns(7)
            w_mon = wc[0].number_input("月", min_value=0, value=dw[0], key=f"rw_mon_{key_prefix}")
            w_tue = wc[1].number_input("火", min_value=0, value=dw[1], key=f"rw_tue_{key_prefix}")
            w_wed = wc[2].number_input("水", min_value=0, value=dw[2], key=f"rw_wed_{key_prefix}")
            w_thu = wc[3].number_input("木", min_value=0, value=dw[3], key=f"rw_thu_{key_prefix}")
            w_fri = wc[4].number_input("金", min_value=0, value=dw[4], key=f"rw_fri_{key_prefix}")
            w_sat = wc[5].number_input("土", min_value=0, value=dw[5], key=f"rw_sat_{key_prefix}")
            w_sun = wc[6].number_input("日", min_value=0, value=dw[6], key=f"rw_sun_{key_prefix}")
            new_weights = [w_mon, w_tue, w_wed, w_thu, w_fri, w_sat, w_sun]

        if max_delay_days <= 2:
            st.info(f"💡 {delayed_count}件のタスクが1〜2日遅れています。")
            if st.button("🔄 スケジュールを自動微調整する", key=f"resch_mild_{key_prefix}"):
                smart_reschedule(new_target, new_weights)
                st.session_state.reschedule_success = True
                st.rerun()
        elif 3 <= max_delay_days <= 6:
            st.warning(f"⚠️ {max_delay_days}日の遅れが発生しています（計 {delayed_count}件）。\n予定が詰まってきているため、計画全体を再計算して無理のないペースに戻すことをおすすめします。")
            if st.button("🔄 計画をスマートに再計算する", key=f"resch_mod_{key_prefix}"):
                smart_reschedule(new_target, new_weights)
                st.session_state.reschedule_success = True
                st.rerun()
        else:
            st.error(f"🚨 {max_delay_days}日以上の大幅な遅れがあります（計 {delayed_count}件）。")
            st.write(f"このままでは目標に間に合わない可能性があります。復習を最優先に処理し、残り {new_tasks_count} 件の新規タスクを期限まで再配分して立て直しましょう。")
            if st.button("🔥 ペースを再設定して計画を立て直す", type="primary", key=f"resch_sev_{key_prefix}"):
                smart_reschedule(new_target, new_weights)
                st.session_state.reschedule_success = True
                st.rerun()
        st.divider()
    else:
        if key_prefix == "cal":
            return
            
        st.success("🎉 現在、予定より遅れているタスクはありません。完璧なペースです！")
        if new_tasks_count > 0:
            with st.expander("🔄 計画の再配分 (リスケジュール)"):
                st.write(f"残りの未学習タスク ({new_tasks_count}件) を、指定した目標日と曜日ウェイトで再配置します。")
                c_date_ok, c_weights_ok = st.columns([1, 2])
                with c_date_ok:
                    new_target = st.date_input("完了目標日", value=default_target, key=f"resch_ok_date_{key_prefix}")
                with c_weights_ok:
                    st.write("曜日ウェイト")
                    wc_ok = st.columns(7)
                    w_mon = wc_ok[0].number_input("月", min_value=0, value=dw[0], key=f"rw_ok_mon_{key_prefix}")
                    w_tue = wc_ok[1].number_input("火", min_value=0, value=dw[1], key=f"rw_ok_tue_{key_prefix}")
                    w_wed = wc_ok[2].number_input("水", min_value=0, value=dw[2], key=f"rw_ok_wed_{key_prefix}")
                    w_thu = wc_ok[3].number_input("木", min_value=0, value=dw[3], key=f"rw_ok_thu_{key_prefix}")
                    w_fri = wc_ok[4].number_input("金", min_value=0, value=dw[4], key=f"rw_ok_fri_{key_prefix}")
                    w_sat = wc_ok[5].number_input("土", min_value=0, value=dw[5], key=f"rw_ok_sat_{key_prefix}")
                    w_sun = wc_ok[6].number_input("日", min_value=0, value=dw[6], key=f"rw_ok_sun_{key_prefix}")
                    new_weights = [w_mon, w_tue, w_wed, w_thu, w_fri, w_sat, w_sun]

                if st.button("計画を再計算する", key=f"resch_ok_{key_prefix}"):
                    smart_reschedule(new_target, new_weights)
                    st.session_state.reschedule_success = True
                    st.rerun()
        st.divider()

def import_excel_schedule(df, start_date, target_date, day_weights, exam_date_input):
    conn = get_db_connection()
    c = conn.cursor()
    
    valid_tasks = []
    for col in df.columns:
        if col in ['時期', 'フェーズ', 'Column1', 'Column2']: 
            continue
        subject = str(col)
        for item in df[col].dropna():
            task_desc = str(item)
            if task_desc.strip() != '-' and task_desc.strip() != '' and task_desc != 'nan':
                valid_tasks.append((subject, task_desc))
                
    total_tasks = len(valid_tasks)
    if total_tasks == 0:
        return 0, 0
        
    slots = []
    current_date = start_date
    while current_date <= target_date:
        weekday = current_date.weekday()
        weight = day_weights[weekday]
        slots.extend([current_date] * weight)
        current_date += timedelta(days=1)
        
    if not slots:
        slots = [start_date]

    subject_tasks = {}
    for subj, desc in valid_tasks:
        if subj not in subject_tasks:
            subject_tasks[subj] = []
        subject_tasks[subj].append(desc)
        
    mixed_tasks = []
    max_len = max(len(lst) for lst in subject_tasks.values())
    for i in range(max_len):
        for subj in subject_tasks.keys():
            if i < len(subject_tasks[subj]):
                mixed_tasks.append((subj, subject_tasks[subj][i]))

    db_tasks = []
    for i, (subj, desc) in enumerate(mixed_tasks):
        if total_tasks > 1:
            slot_index = int((i / (total_tasks - 1)) * (len(slots) - 1))
        else:
            slot_index = 0
            
        assigned_date = slots[slot_index]
        
        db_tasks.append((
            subj, 
            desc, 
            assigned_date.strftime('%Y-%m-%d'), 
            1, 
            2.5,
            0
        ))
        
    c.execute('''
        INSERT INTO settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    ''', ('exam_date', exam_date_input.strftime('%Y-%m-%d')))
        
    c.executemany('INSERT INTO tasks (subject, topic, next_review, interval, ease_factor, is_review) VALUES (%s, %s, %s, %s, %s, %s)', db_tasks)
    conn.commit()
    conn.close()
    
    days_used = (target_date - start_date).days
    return total_tasks, days_used

def update_task_date(task_id, new_date):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE tasks SET next_review = %s WHERE id = %s', (new_date, task_id))
    conn.commit()
    conn.close()

def delete_task(task_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('DELETE FROM tasks WHERE id = %s', (task_id,))
    c.execute('DELETE FROM history WHERE task_id = %s', (task_id,))
    conn.commit()
    conn.close()

# --- ダイアログ ---
@st.dialog("📝 タスク詳細・編集")
def task_detail_dialog(props):
    task_id = props["task_id"]
    status_text = "🔄 復習 (維持フェーズ)" if props.get('is_review', 0) == 1 else "🆕 新規着手 (初回学習)"
    
    st.write(f"**ステータス:** {status_text}")
    st.write(f"**科目:** {props['subject']}")
    st.write(f"**内容:** {props['topic']}")
    st.write(f"**現在の定着度(Ease Factor):** {props['ease_factor']}")
    
    st.divider()
    
    st.write("▼ 復習を実行する（理解度を選択）")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("Again", key=f"dlg_again_{task_id}", use_container_width=True):
        update_task(task_id, 'Again', props['interval'], props['ease_factor'])
        st.rerun()
    if c2.button("Hard", key=f"dlg_hard_{task_id}", use_container_width=True):
        update_task(task_id, 'Hard', props['interval'], props['ease_factor'])
        st.rerun()
    if c3.button("Good", key=f"dlg_good_{task_id}", use_container_width=True):
        update_task(task_id, 'Good', props['interval'], props['ease_factor'])
        st.rerun()
    if c4.button("Easy", key=f"dlg_easy_{task_id}", use_container_width=True):
        update_task(task_id, 'Easy', props['interval'], props['ease_factor'])
        st.rerun()
            
    st.divider()
    
    st.write("▼ 予定の微調整・削除")
    current_date_obj = datetime.strptime(props["start_date"], '%Y-%m-%d').date()
    
    c5, c6 = st.columns([2, 1])
    with c5:
        new_date = st.date_input("予定日の変更", value=current_date_obj, key=f"dlg_date_{task_id}")
        if st.button("日付を更新", type="primary", key=f"dlg_update_{task_id}", use_container_width=True):
            update_task_date(task_id, new_date.strftime('%Y-%m-%d'))
            st.rerun()
    with c6:
        st.write("") 
        st.write("") 
        if st.button("タスク削除", key=f"dlg_delete_{task_id}", use_container_width=True):
            delete_task(task_id)
            st.rerun()

# --- UI構築 (Streamlit) ---
st.set_page_config(page_title="復習管理システム", layout="wide")

# DB初期化（例外処理を追加してエラーを見やすく）
try:
    init_db()
except Exception as e:
    st.warning("データベースのセットアップが完了していません。設定を行ってください。")
    st.stop()

def get_exam_date():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = 'exam_date'")
    result = c.fetchone()
    conn.close()
    if result:
        return datetime.strptime(result[0], '%Y-%m-%d').date()
    return None

exam_date = get_exam_date()
with st.sidebar:
    st.header("🎯 本番までのカウントダウン")
    if exam_date:
        today_date = datetime.now().date()
        days_to_exam = (exam_date - today_date).days
        if days_to_exam > 0:
            st.metric(label="試験日まで残り", value=f"{days_to_exam} 日", delta=f"目標日: {exam_date.strftime('%Y年%m月%d日')}", delta_color="off")
        elif days_to_exam == 0:
            st.error("🔥 本日はいよいよ試験本番です！健闘を祈ります！")
        else:
            st.write("試験日は終了しました。")
    else:
        st.info("試験日が設定されていません。「Excel取り込み」タブから設定できます。")

st.title("📚 忘却曲線 学習マネージャー")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📅 カレンダー", "📝 タスク追加・逆算", "📂 Excel取り込み", "📊 ダッシュボード", "🗄️ データ一覧"])

# --- タブ1: 📅 カレンダー ---
with tab1:
    st.header("学習カレンダー")
    
    render_reschedule_alert("cal")
    
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM tasks", conn)
    conn.close()
    
    events = []
    if not df.empty:
        for index, row in df.iterrows():
            if "財務" in row['subject']: color = "#e74c3c"
            elif "管理" in row['subject']: color = "#2ecc71"
            elif "監査" in row['subject']: color = "#f1c40f"
            elif "企業" in row['subject']: color = "#9b59b6"
            elif "租税" in row['subject']: color = "#e67e22"
            elif "経営" in row['subject']: color = "#34495e"
            else: color = "#95a5a6"

            icon = "🔄" if row.get('is_review', 0) == 1 else "🆕"
            topic_str = str(row['topic'])
            display_topic = topic_str[:10] + "..." if len(topic_str) > 10 else topic_str
            
            # PostgreSQLのDate型を文字列に変換
            start_str = row['next_review'].strftime('%Y-%m-%d') if not isinstance(row['next_review'], str) else row['next_review']

            events.append({
                "title": f"{icon} {row['subject']}: {display_topic}",
                "start": start_str,
                "backgroundColor": color,
                "borderColor": color,
                "extendedProps": {
                    "task_id": row['id'],
                    "subject": row['subject'],
                    "topic": row['topic'],
                    "interval": row['interval'],
                    "ease_factor": row['ease_factor'],
                    "is_review": row.get('is_review', 0),
                    "start_date": start_str
                }
            })

    calendar_options = {
        "headerToolbar": {
            "left": "today prev,next",
            "center": "title",
            "right": "dayGridMonth,listWeek",
        },
        "initialView": "dayGridMonth",
        "locale": "ja",
        "selectable": True,
        "height": 650,          
        "dayMaxEvents": 3,      
    }

    custom_css = """
        .fc-event-title { 
            font-weight: normal; 
            font-size: 0.85em; 
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap; 
        }
    """
    state = calendar(events=events, options=calendar_options, custom_css=custom_css, key="study_calendar")

    if state.get("eventClick"):
        clicked_event = state["eventClick"]["event"]
        props = clicked_event["extendedProps"]
        task_detail_dialog(props)

# --- タブ2: 📝 タスク追加・逆算 ---
with tab2:
    st.header("📝 新規タスクの追加・逆算スケジュール生成")
    st.write("単発のタスクを登録するか、問題数などを目標日から逆算して自動配分します。")
    
    task_type = st.radio("登録タイプを選択", ["🎯 複数タスクを目標日から逆算して自動配分", "📌 単発タスクを個別登録"], horizontal=True)
    
    st.divider()

    if task_type == "🎯 複数タスクを目標日から逆算して自動配分":
        st.subheader("目標逆算スケジュールの生成")
        
        with st.form("bulk_task_form", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                bulk_subject = st.selectbox("科目", ["財務会計論", "管理会計論", "監査論", "企業法", "租税法", "経営学", "その他"])
                bulk_base_name = st.text_input("タスクの基本名 (例: テキスト, 短答問題集)", "テキスト")
                
                c1, c2 = st.columns(2)
                with c1:
                    bulk_total = st.number_input("全体の量(章・回数)", min_value=1, value=1)
                with c2:
                    completed_count_raw = st.number_input("うち学習済みの数", min_value=0, value=0)
                
            with col2:
                start_date_bulk = st.date_input("開始日（未学習分を着手する日）", value=datetime.now().date())
                default_target_bulk = datetime.now().date() + timedelta(days=90)
                target_date_bulk = st.date_input("完了目標日", value=default_target_bulk)
                
            st.write("▼ 未学習タスクの曜日ごとの配分ウェイト（比重）")
            bc1, bc2, bc3, bc4, bc5, bc6, bc7 = st.columns(7)
            with bc1: bw_mon = st.number_input("月", min_value=0, value=1)
            with bc2: bw_tue = st.number_input("火", min_value=0, value=1)
            with bc3: bw_wed = st.number_input("水", min_value=0, value=1)
            with bc4: bw_thu = st.number_input("木", min_value=0, value=1)
            with bc5: bw_fri = st.number_input("金", min_value=0, value=1)
            with bc6: bw_sat = st.number_input("土", min_value=0, value=3)
            with bc7: bw_sun = st.number_input("日", min_value=0, value=3)
            bulk_weights = [bw_mon, bw_tue, bw_wed, bw_thu, bw_fri, bw_sat, bw_sun]
            
            bulk_submitted = st.form_submit_button("逆算スケジュールを生成する", type="primary")
            
            if bulk_submitted:
                completed_count = min(completed_count_raw, bulk_total)
                
                if target_date_bulk <= start_date_bulk:
                    st.error("エラー：完了目標日は開始日より後にしてください。")
                elif sum(bulk_weights) == 0 and (bulk_total - completed_count) > 0:
                    st.error("エラー：すべてのウェイトを0にすることはできません。")
                else:
                    conn = get_db_connection()
                    c = conn.cursor()
                    db_tasks = []
                    today_str = datetime.now().strftime('%Y-%m-%d')
                    
                    for i in range(completed_count):
                        topic_name = f"{bulk_base_name} - 第{i+1}回"
                        db_tasks.append((bulk_subject, topic_name, today_str, 1, 2.5, 1))
                    
                    remaining_count = bulk_total - completed_count
                    if remaining_count > 0:
                        slots = []
                        current_date = start_date_bulk
                        while current_date <= target_date_bulk:
                            weight = bulk_weights[current_date.weekday()]
                            slots.extend([current_date] * weight)
                            current_date += timedelta(days=1)
                        if not slots: slots = [start_date_bulk]
                        
                        for j in range(remaining_count):
                            topic_index = completed_count + j
                            if remaining_count > 1:
                                slot_index = int((j / (remaining_count - 1)) * (len(slots) - 1))
                            else:
                                slot_index = 0
                                
                            assigned_date = slots[slot_index]
                            topic_name = f"{bulk_base_name} - 第{topic_index+1}回"
                            db_tasks.append((bulk_subject, topic_name, assigned_date.strftime('%Y-%m-%d'), 1, 2.5, 0))
                            
                    c.executemany('INSERT INTO tasks (subject, topic, next_review, interval, ease_factor, is_review) VALUES (%s, %s, %s, %s, %s, %s)', db_tasks)
                    conn.commit()
                    conn.close()
                    
                    if completed_count_raw > bulk_total:
                        st.warning(f"※学習済みの数が全体の量を超えていたため、自動的に「{bulk_total}」件として処理しました。")
                    
                    st.success(f"【登録完了】学習済み {completed_count} 件を復習に回し、未学習 {remaining_count} 件を目標日まで割り振りました！")

    else:
        st.subheader("単発タスクの個別登録")
        with st.form("single_task_form", clear_on_submit=True):
            single_subject = st.selectbox("科目", ["財務会計論", "管理会計論", "監査論", "企業法", "租税法", "経営学", "その他"])
            single_topic = st.text_input("学習項目 (例: 第3回 模試の復習)")
            single_date = st.date_input("実行予定日", value=datetime.now().date())
            single_status = st.radio("現在の状態", ["🆕 新規着手", "🔄 すでに学習済み（復習として追加）"], horizontal=True)
            
            submitted = st.form_submit_button("登録")
            if submitted and single_topic:
                is_rev = 1 if single_status == "🔄 すでに学習済み（復習として追加）" else 0
                
                conn = get_db_connection()
                c = conn.cursor()
                c.execute('INSERT INTO tasks (subject, topic, next_review, interval, ease_factor, is_review) VALUES (%s, %s, %s, %s, %s, %s)',
                          (single_subject, single_topic, single_date.strftime('%Y-%m-%d'), 1, 2.5, is_rev))
                conn.commit()
                conn.close()
                
                st.success(f"【登録完了】「{single_subject} - {single_topic}」を {single_date.strftime('%Y-%m-%d')} の予定として登録しました！")
                
# --- タブ3: 📂 Excel取り込み ---
with tab3:
    st.header("📂 教材リスト(Excel)の一括スケジュール化")
    st.write("科目のタスクリストを読み込み、曜日ごとの学習ウェイトを考慮して期間内に自動配分します。")
    
    if 'uploader_key' not in st.session_state:
        st.session_state.uploader_key = 0
    if 'success_msg' not in st.session_state:
        st.session_state.success_msg = None

    if st.session_state.success_msg:
        st.success(st.session_state.success_msg)
        st.session_state.success_msg = None 

    uploaded_file = st.file_uploader("タスクリストのExcelファイルをアップロード", type=["xlsx", "xls"], key=f"uploader_{st.session_state.uploader_key}")
    
    st.subheader("1. スケジュールの期間設定")
    col1, col2, col3 = st.columns(3)
    with col1:
        start_date_excel = st.date_input("開始日（着手開始）", value=datetime.now().date())
    with col2:
        default_target = datetime.now().date() + timedelta(days=90)
        target_date_excel = st.date_input("1周完了の目標日", value=default_target)
    with col3:
        default_exam = exam_date if exam_date else (datetime.now().date() + timedelta(days=200))
        exam_date_input = st.date_input("🏆 最終試験日 (本番)", value=default_exam)
        
    st.subheader("2. 曜日ごとの学習ウェイト（比重）設定")
    
    wc1, wc2, wc3, wc4, wc5, wc6, wc7 = st.columns(7)
    with wc1: w_mon = st.number_input("月", min_value=0, value=1)
    with wc2: w_tue = st.number_input("火", min_value=0, value=1)
    with wc3: w_wed = st.number_input("水", min_value=0, value=1)
    with wc4: w_thu = st.number_input("木", min_value=0, value=1)
    with wc5: w_fri = st.number_input("金", min_value=0, value=1)
    with wc6: w_sat = st.number_input("土", min_value=0, value=3) 
    with wc7: w_sun = st.number_input("日", min_value=0, value=3)
    
    day_weights = [w_mon, w_tue, w_wed, w_thu, w_fri, w_sat, w_sun]

    if uploaded_file is not None:
        try:
            df_import = pd.read_excel(uploaded_file)
            st.dataframe(df_import.head(3), use_container_width=True)
            
            if st.button("この設定でスケジュールを生成する", type="primary"):
                if target_date_excel <= start_date_excel:
                    st.error("エラー：1周完了の目標日は開始日より後にしてください。")
                elif sum(day_weights) == 0:
                    st.error("エラー：すべてのウェイトを0にすることはできません。")
                else:
                    added_count, days_used = import_excel_schedule(df_import, start_date_excel, target_date_excel, day_weights, exam_date_input)
                    
                    if added_count > 0:
                        st.session_state.success_msg = f"✅ 合計 {added_count} 個のタスクを割り振りました！"
                        st.session_state.uploader_key += 1 
                        st.rerun()
                    else:
                        st.warning("有効なタスクが見つかりませんでした。")
        except Exception as e:
            st.error(f"エラーが発生しました: {e}")

# --- タブ4: ダッシュボード（グラフ化） ---
with tab4:
    st.header("📊 学習ダッシュボード")
    
    render_reschedule_alert("dash")
    
    conn = get_db_connection()
    df = pd.read_sql_query("SELECT * FROM tasks", conn)
    conn.close()

    if df.empty:
        st.info("データがありません。まずは「タスク追加」から学習計画を登録してください。")
    else:
        today_date = datetime.now().date()
        today_str = today_date.strftime('%Y-%m-%d')
        
        total_tasks = len(df)
        reviewed_tasks = len(df[df['is_review'] == 1])
        learning_rate = (reviewed_tasks / total_tasks * 100) if total_tasks > 0 else 0
        
        retention_tasks = len(df[(df['is_review'] == 1) & (df['interval'] >= 15)])
        retention_rate = (retention_tasks / reviewed_tasks * 100) if reviewed_tasks > 0 else 0
        
        # PostgreSQLからの読み込み時に日付型を文字列として扱うための変換
        df['next_review'] = pd.to_datetime(df['next_review']).dt.strftime('%Y-%m-%d')
        debt_df = df[df['next_review'] < today_str].copy()
        
        total_debt_tasks = len(debt_df)
        total_debt_days = 0
        if not debt_df.empty:
            today_pd = pd.to_datetime(today_str)
            debt_df['next_review_dt'] = pd.to_datetime(debt_df['next_review'])
            debt_df['delay_days'] = (today_pd - debt_df['next_review_dt']).dt.days
            total_debt_days = int(debt_df['delay_days'].sum())
            
            def categorize_debt(row):
                if row['is_review'] == 0:
                    return "🆕 新規未着手 (遅延)"
                else:
                    days = row['delay_days']
                    if days >= 7: return "🔴 重度 (7日以上)"
                    elif days >= 3: return "🟠 中度 (3〜6日)"
                    else: return "🟡 軽度 (1〜2日)"
                    
            debt_df['debt_level'] = debt_df.apply(categorize_debt, axis=1)

        st.subheader("🔥 総合学習スコア")
        m1, m2, m3 = st.columns(3)
        with m1:
            st.metric("📊 学習率 (着手進捗)", f"{learning_rate:.1f}%", f"{reviewed_tasks} / {total_tasks} 件完了")
        with m2:
            st.metric("🧠 定着率 (長期記憶化)", f"{retention_rate:.1f}%", f"{retention_tasks} 件マスター")
        with m3:
            st.metric("💸 復習負債・遅延合計", f"{total_debt_days} 日分", f"対象 {total_debt_tasks} 件", delta_color="inverse")

        st.divider()

        st.subheader("💸 復習負債 (Review Backlog) ダッシュボード")
        st.write("予定より遅れているタスクです。焦る必要はありませんが、重度のものや新規の遅延から順に消化していくと効率的です。")

        if debt_df.empty:
            st.success("🎉 現在、復習負債はゼロです！完璧なペースで学習できています。")
        else:
            d_col1, d_col2 = st.columns(2)
            
            with d_col1:
                st.write("▼ 負債の重症度別 内訳")
                level_counts = debt_df['debt_level'].value_counts().reset_index()
                level_counts.columns = ['重症度', '件数']
                
                level_order = ["🔴 重度 (7日以上)", "🟠 中度 (3〜6日)", "🟡 軽度 (1〜2日)", "🆕 新規未着手 (遅延)"]
                color_map_debt = {
                    "🔴 重度 (7日以上)": "#e74c3c", 
                    "🟠 中度 (3〜6日)": "#f39c12", 
                    "🟡 軽度 (1〜2日)": "#f1c40f",
                    "🆕 新規未着手 (遅延)": "#3498db"
                }
                
                fig_debt_pie = px.pie(level_counts, values='件数', names='重症度', hole=0.5,
                                      color='重症度', color_discrete_map=color_map_debt,
                                      category_orders={"重症度": level_order})
                st.plotly_chart(fig_debt_pie, use_container_width=True, key="debt_pie_chart_app")

            with d_col2:
                st.write("▼ 科目別の負債状況 (遅延日数合計)")
                subj_debt = debt_df.groupby('subject')['delay_days'].sum().reset_index()
                subj_debt = subj_debt.sort_values('delay_days', ascending=True)
                
                fig_debt_bar = px.bar(subj_debt, x='delay_days', y='subject', orientation='h',
                                      labels={'delay_days': '遅延日数の合計', 'subject': '科目'})
                st.plotly_chart(fig_debt_bar, use_container_width=True, key="debt_bar_chart_app")

            st.write("▼ 返済優先順位リスト（遅延が長く、苦手なもの順）")
            priority_debt = debt_df.sort_values(by=['delay_days', 'ease_factor'], ascending=[False, True]).head(10)
            
            display_debt = priority_debt[['subject', 'topic', 'delay_days', 'debt_level', 'ease_factor']].copy()
            display_debt.columns = ['科目', 'タスク内容', '遅延日数', 'レベル', '現在の定着度(Ease)']
            
            st.dataframe(display_debt, use_container_width=True, hide_index=True)

        st.divider()

        st.subheader("📈 科目別の習熟度ポートフォリオ")
        
        def categorize_phase(row):
            if row.get('is_review', 0) == 0: return "1_未着手 (インプット待ち)"
            elif row['interval'] <= 3: return "2_短期記憶 (1〜3日間隔)"
            elif row['interval'] <= 14: return "3_中期記憶 (4〜14日間隔)"
            else: return "4_長期記憶 (15日以上間隔)"
        
        df['フェーズ'] = df.apply(categorize_phase, axis=1)
        phase_df = df.groupby(['subject', 'フェーズ']).size().reset_index(name='タスク数')
        
        phase_color_map = {
            "1_未着手 (インプット待ち)": "#e0e0e0",
            "2_短期記憶 (1〜3日間隔)": "#e74c3c",
            "3_中期記憶 (4〜14日間隔)": "#f39c12",
            "4_長期記憶 (15日以上間隔)": "#3498db"
        }
        
        fig_portfolio = px.bar(
            phase_df, x='タスク数', y='subject', color='フェーズ',
            orientation='h', color_discrete_map=phase_color_map,
            category_orders={"フェーズ": list(phase_color_map.keys())}
        )
        fig_portfolio.update_layout(barmode='stack', yaxis={'categoryorder': 'total ascending'})
        st.plotly_chart(fig_portfolio, use_container_width=True, key="portfolio_chart_app")

# --- タブ5: 🗄️ 全データの一覧 ---
with tab5:
    st.header("🗄️ すべての学習データ")
    
    with st.expander("⚠️ データベースの初期化（全データ削除）"):
        st.warning("この操作は取り消せません。すべての学習記録と予定が削除されます。")
        if st.button("全データを削除する", type="primary"):
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("DELETE FROM tasks")
            c.execute("DELETE FROM settings")
            c.execute("DELETE FROM history") 
            conn.commit()
            conn.close()
            st.success("データベースを空にしました。")
            st.rerun()

    conn = get_db_connection()
    all_data = pd.read_sql_query("SELECT * FROM tasks", conn)
    conn.close()
    
    if not all_data.empty:
        if 'id' in all_data.columns:
            display_data = all_data.drop(columns=['id'])
        else:
            display_data = all_data
            
        st.dataframe(display_data, use_container_width=True)
    else:
        st.info("現在登録されているデータはありません。")
