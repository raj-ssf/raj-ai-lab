"""
Qdrant snapshot manager — backup/restore collections to MinIO on model swap.

Before deleting a collection (dimension mismatch), snapshots it to MinIO
tagged with the model dimensions. On restore, checks if a snapshot exists
for the current dimensions and restores instead of requiring re-ingestion.
"""
import io
import json
import os
import urllib.error
import urllib.request

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant.ai-data:6333")
MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "minio.ai-data:9000")
MINIO_ACCESS = os.environ.get("MINIO_ACCESS_KEY", "rajailab")
MINIO_SECRET = os.environ.get("MINIO_SECRET_KEY", "rajailab123")
BACKUP_BUCKET = "qdrant-snapshots"


def _get_minio():
    try:
        from minio import Minio
        s3 = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS, secret_key=MINIO_SECRET, secure=False)
        if not s3.bucket_exists(BACKUP_BUCKET):
            s3.make_bucket(BACKUP_BUCKET)
        return s3
    except:
        return None


def snapshot_collection(collection_name, dimensions):
    """Snapshot a collection and upload to MinIO. Returns True on success."""
    s3 = _get_minio()
    if not s3:
        print(f"[snapshot] MinIO unavailable — skipping backup of {collection_name}")
        return False

    try:
        # Create snapshot via Qdrant API
        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{collection_name}/snapshots",
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=30)
        snap_info = json.loads(resp.read()).get("result", {})
        snap_name = snap_info.get("name", "")

        if not snap_name:
            print(f"[snapshot] Failed to create snapshot for {collection_name}")
            return False

        # Download snapshot
        snap_url = f"{QDRANT_URL}/collections/{collection_name}/snapshots/{snap_name}"
        snap_data = urllib.request.urlopen(snap_url, timeout=120).read()

        # Upload to MinIO: qdrant-snapshots/{collection}/{dim}/{snapshot-file}
        s3_key = f"{collection_name}/dim-{dimensions}/{snap_name}"
        s3.put_object(BACKUP_BUCKET, s3_key, io.BytesIO(snap_data), len(snap_data))
        print(f"[snapshot] Backed up {collection_name} ({len(snap_data)} bytes) → s3://{BACKUP_BUCKET}/{s3_key}")

        # Clean up snapshot from Qdrant server
        try:
            del_req = urllib.request.Request(
                f"{QDRANT_URL}/collections/{collection_name}/snapshots/{snap_name}",
                method="DELETE"
            )
            urllib.request.urlopen(del_req, timeout=10)
        except:
            pass

        return True

    except Exception as e:
        print(f"[snapshot] Backup failed for {collection_name}: {e}")
        return False


def restore_collection(collection_name, dimensions):
    """Restore a collection from MinIO snapshot if one exists for these dimensions. Returns True on success."""
    s3 = _get_minio()
    if not s3:
        return False

    try:
        prefix = f"{collection_name}/dim-{dimensions}/"
        snapshots = list(s3.list_objects(BACKUP_BUCKET, prefix=prefix))

        if not snapshots:
            print(f"[snapshot] No backup found for {collection_name} at dim={dimensions}")
            return False

        # Use the latest snapshot
        latest = sorted(snapshots, key=lambda o: o.last_modified, reverse=True)[0]
        snap_key = latest.object_name
        print(f"[snapshot] Found backup: s3://{BACKUP_BUCKET}/{snap_key}")

        # Download from MinIO
        response = s3.get_object(BACKUP_BUCKET, snap_key)
        snap_data = response.read()
        response.close()
        response.release_conn()

        # Upload snapshot to Qdrant via multipart form upload using urllib
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".snapshot", delete=False) as f:
            f.write(snap_data)
            temp_path = f.name

        boundary = "----SnapshotBoundary"
        filename = os.path.basename(snap_key)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="snapshot"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + snap_data + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            f"{QDRANT_URL}/collections/{collection_name}/snapshots/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST"
        )

        try:
            resp = urllib.request.urlopen(req, timeout=120)
            result = json.loads(resp.read())
            os.unlink(temp_path)
            if result.get("status") == "ok" or result.get("result"):
                print(f"[snapshot] Restored {collection_name} from backup ({len(snap_data)} bytes)")
                return True
            else:
                print(f"[snapshot] Restore response: {result}")
                return False
        except urllib.error.HTTPError as e:
            os.unlink(temp_path)
            print(f"[snapshot] Restore HTTP error: {e.code} {e.read().decode()[:200]}")
            return False

    except Exception as e:
        print(f"[snapshot] Restore failed for {collection_name}: {e}")
        return False
