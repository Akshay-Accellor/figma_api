import os
import json
import re
import logging
import requests
from flask import Flask, request, jsonify, abort
from flask import send_file

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configuration
BASE_URL = os.getenv("FIGMA_BASE_URL", "https://api.figma.com/v1")
FIGMA_API_TOKEN = os.getenv("FIGMA_API_TOKEN")
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "/app/uploads")

# Ensure upload folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Figma processing functions
def extract_file_key(figma_url):
    pattern = r"figma\.com/(?:file|design)/([a-zA-Z0-9_-]+)"
    match = re.search(pattern, figma_url)
    
    if match:
        return match.group(1)
    else:
        board_pattern = r"figma\.com/board/([a-zA-Z0-9_-]+)"
        board_match = re.search(board_pattern, figma_url)
        
        if board_match:
            return board_match.group(1)
        else:
            raise ValueError("Invalid Figma URL")

def fetch_figma_file(file_key):
    headers = {"X-Figma-Token": FIGMA_API_TOKEN}
    try:
        response = requests.get(f"{BASE_URL}/files/{file_key}", headers=headers)
        if response.status_code == 403:
            logger.error(f"403 Forbidden: {response.text}")
            raise Exception(f"403 Forbidden: {response.text}")
        elif response.status_code != 200:
            logger.error(f"Figma API error (Status {response.status_code}): {response.text}")
            raise Exception(f"Figma API error (Status {response.status_code}): {response.text}")
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Network error: {str(e)}")
        raise Exception(f"Network error: {str(e)}")

def fetch_figma_frame_images(file_key, frame_ids, scale=1, format="png"):
    headers = {"X-Figma-Token": FIGMA_API_TOKEN}
    max_ids_per_request = 100
    image_urls = {}
    
    for i in range(0, len(frame_ids), max_ids_per_request):
        batch_ids = frame_ids[i:i + max_ids_per_request]
        params = {
            "ids": ",".join(batch_ids),
            "scale": scale,
            "format": format,
            "use_absolute_bounds": "true"
        }
        try:
            response = requests.get(f"{BASE_URL}/images/{file_key}", headers=headers, params=params)
            if response.status_code == 403:
                raise Exception("Access denied: Check token permissions")
            elif response.status_code != 200:
                raise Exception(f"Figma API error: {response.text}")
            data = response.json()
            if data.get("err"):
                raise Exception(f"Image rendering error: {data['err']}")
            image_urls.update(data.get("images", {}))
        except requests.RequestException as e:
            raise Exception(f"Network error: {str(e)}")
    
    return image_urls

def extract_frames(file_data):
    frames = []
    frame_ids = []
    document = file_data.get("document", {})
    pages = document.get("children", [])
    
    for page in pages:
        for frame in page.get("children", []):
            if frame.get("type") != "FRAME":
                continue
            frame_data = {
                "name": frame.get("name", "Unnamed Frame"),
                "node_id": frame.get("id", ""),
                "type": "FRAME"
            }
            frames.append(frame_data)
            if frame.get("id"):
                frame_ids.append(frame.get("id"))
    
    return frames, frame_ids

def download_images(frames, session_id):
    image_paths = {}
    frame_folder = os.path.join(UPLOAD_FOLDER, "frames")
    os.makedirs(frame_folder, exist_ok=True)
    
    session_folder = os.path.join(frame_folder, session_id)
    os.makedirs(session_folder, exist_ok=True)
    
    for frame in frames:
        if "image_url" not in frame:
            continue
        
        try:
            response = requests.get(frame["image_url"])
            if response.status_code != 200:
                logger.warning(f"Failed to download image for frame '{frame['name']}': HTTP {response.status_code}")
                continue
            
            safe_node_id = re.sub(r'[^\w\-_.]', '_', frame['node_id'])
            filename = os.path.join(session_folder, f"{safe_node_id}.png")
            
            with open(filename, 'wb') as f:
                f.write(response.content)
            
            image_paths[frame["node_id"]] = filename
            logger.debug(f"Downloaded image for frame '{frame['name']}' to {filename}")
            
        except Exception as e:
            logger.error(f"Error downloading image for frame '{frame['name']}': {str(e)}")
    
    return image_paths, session_folder

# API endpoint to process Figma URL
@app.route("/process-figma", methods=["POST"])
def process_figma():
    try:
        data = request.get_json()
        if not data or 'figma_url' not in data or 'session_id' not in data:
            abort(400, description="Missing figma_url or session_id")
        
        figma_url = data['figma_url']
        session_id = data['session_id']
        
        file_key = extract_file_key(figma_url)
        file_data = fetch_figma_file(file_key)
        frames, frame_ids = extract_frames(file_data)
        image_urls = fetch_figma_frame_images(file_key, frame_ids)
        
        for frame in frames:
            frame['image_url'] = image_urls.get(frame['node_id'], '')
        
        total_frames = len([frame for frame in frames if frame.get('image_url')])
        image_paths, session_folder = download_images(frames, session_id)
        
        result = {
            'total_frames': total_frames,
            'images': [
                {
                    'name': frame['name'],
                    'node_id': frame['node_id'],
                    'path': image_paths.get(frame['node_id'], '')
                }
                for frame in frames if frame['node_id'] in image_paths
            ],
            'session_folder': session_folder
        }
        
        return jsonify({"status": "success", "result": result})
    
    except Exception as e:
        logger.error(f"Error processing Figma URL: {str(e)}")
        abort(500, description=str(e))

# Endpoint to serve images
@app.route("/images/<session_id>/<node_id>", methods=["GET"])
def get_image(session_id, node_id):
    safe_node_id = re.sub(r'[^\w\-_.]', '_', node_id)
    image_path = os.path.join(UPLOAD_FOLDER, "frames", session_id, f"{safe_node_id}.png")
    if not os.path.exists(image_path):
        abort(404, description="Image not found")
    return send_file(image_path, mimetype="image/png")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))