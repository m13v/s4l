import os, json, sys
sys.path.insert(0, os.path.expanduser('~/social-autoposter'))
from dotenv import load_dotenv
load_dotenv(os.path.expanduser('~/social-autoposter/.env'))
import psycopg
DATABASE_URL = os.environ['DATABASE_URL']

caption_path = '/Users/matthewdi/social-autoposter/mixer/remotion/out/post-082.caption.txt'
with open(caption_path, 'r') as f:
    caption_text = f.read()

intro_dur = 4.133
guide_dur = 11.433
impressed_dur = 1.767
result_dur = 7.033
t0 = 0.0
t1 = t0 + intro_dur
t2 = t1 + guide_dur
t3 = t2 + impressed_dur
t4 = t3 + result_dur

source_clips = [
    {'src': 'mixer/intro-studyly-1.mp4', 'order': 0, 'stage': 'intro',
     'start_sec': round(t0, 3), 'end_sec': round(t1, 3),
     'src_dur_sec': intro_dur, 'target_dur_sec': intro_dur, 'speedup': 1.0},
    {'src': 'mixer/guide-studyly.mp4', 'order': 1, 'stage': 'guide',
     'start_sec': round(t1, 3), 'end_sec': round(t2, 3),
     'src_dur_sec': guide_dur, 'target_dur_sec': guide_dur, 'speedup': 1.0},
    {'src': 'mixer/impressed-2.mp4', 'order': 2, 'stage': 'impressed',
     'start_sec': round(t2, 3), 'end_sec': round(t3, 3),
     'src_dur_sec': impressed_dur, 'target_dur_sec': impressed_dur, 'speedup': 1.0},
    {'src': 'mixer/result-studyly-2.mp4', 'order': 3, 'stage': 'finale',
     'start_sec': round(t3, 3), 'end_sec': round(t4, 3),
     'src_dur_sec': result_dur, 'target_dur_sec': result_dur, 'speedup': 1.0},
]

overlays = [
    {'kind': 'title', 'order': 0,
     'text': 'from\n__ACCENT__\non nclex practice (accent=58% to passing; tagline=nursing school rescue)',
     'start_sec': round(t0, 3), 'end_sec': round(t1, 3), 'dur_sec': intro_dur},
    {'kind': 'step', 'order': 1, 'text': 'paste your nclex notes into studyly.io',
     'start_sec': round(t1, 3), 'end_sec': round(t2, 3), 'dur_sec': guide_dur},
    {'kind': 'finale', 'order': 2, 'text': 'and pass the boards',
     'start_sec': round(t3, 3), 'end_sec': round(t4, 3), 'dur_sec': result_dur},
]

metadata = {
    'theme': 'studyly',
    'format': 'mixer',
    'clip_count': 4,
    'source_repo': 'social-autoposter',
    'theme_angle': 'nursing-school-nclex-pharm-rescue',
    'theme_label': 'nursing student rescues nclex pharm score with studyly.io',
    'caption_style': 'ig_studyly_failing_student_arc',
    'overlay_count': 3,
    'composition_id': 'Mixer-studyly-i1-r2',
    'engagement_style': 'ig_studyly_failing_student_arc',
    'description_style': 'failing-student-outcome-arc',
}

with psycopg.connect(DATABASE_URL) as conn:
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO media_posts (
            post_number, project_name, variant_id, target_account,
            status, post_type, video_path, audio_source,
            caption_text, caption_version, duration_sec, width, height,
            source_clips, overlays, metadata
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        RETURNING post_number;
        """, (
            82, 'studyly', 'studyly-i1-r2', 'matthewheartful',
            'draft', 'product',
            '/Users/matthewdi/social-autoposter/mixer/remotion/out/post-082.mp4',
            'local:/Users/matthewdi/social-autoposter/mixer/audio/track-011_iphone-E1760FF1.m4a',
            caption_text, 'v1', 24.4, 1080, 1920,
            json.dumps(source_clips), json.dumps(overlays), json.dumps(metadata),
        ))
        print('Inserted post_number:', cur.fetchone()[0])
        conn.commit()
