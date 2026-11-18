import sys, time
sys.path.insert(0, r"E:\ai-agent-cyber-town\backend\app")
import skills.work.animation.engine as anim
from core.tools import protocol

started = protocol.execute("animation_start", {"prompt": "星空下的小人，走向远处", "style": "watercolor"})
print("  job:", started.get("job_id"))
job_id = started["job_id"]
last = ""
for _ in range(90):
    job = anim.job_status(job_id)
    marker = f"{job.get('stage')}"
    if marker != last:
        print(f"  [{time.strftime('%H:%M:%S')}] {job.get('stage')} {str(job.get('note'))[:30]}")
        last = marker
    if job.get("stage") in ("done", "failed"):
        print("  video:", job.get("video"))
        break
    time.sleep(10)
