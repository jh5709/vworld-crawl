# Web App Console + Wails Desktop for GIS

The pipeline operations console is a React web app with a FastAPI backend. A separate Go/Wails desktop app handles full GIS visualization. The web app includes only minimal map preview (Leaflet or basic DeckGL, feature count + bounding box) for pipeline confirmation.

**Status:** accepted

**Considered Options:**
- Desktop-only: Tauri/Electron app bundling everything. Simpler distribution but the Wails desktop already exists and DeckGL requires a browser context anyway.
- Web-only with full GIS: duplicates the Wails desktop's functionality and adds unnecessary complexity.
- Web console + Wails viewer: each tool does what it's best at. Web app handles crawling, pipeline orchestration, and DuckLake management. Wails desktop handles interactive GIS.

**Consequences:**
- The web app must emit WKB geometry + bounding box columns for the Wails desktop to consume efficiently.
- Real-time pipeline progress streams via WebSocket to the web GUI.
- The web app is the trigger point; the Wails desktop is downstream read-only.
