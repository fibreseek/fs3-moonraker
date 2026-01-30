# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Moonraker is a Python-based HTTP/WebSocket API server that acts as a bridge between web clients (Mainsail, Fluidd, KlipperScreen) and the Klipper 3D printing firmware. It communicates with Klipper over a Unix Domain Socket and exposes comprehensive APIs for printer control, file management, system monitoring, and extensibility.

## Development Commands

### Dependencies
- Project uses PDM (Python Dependency Management) for packaging
- Dependencies are defined in `pyproject.toml`
- Install development dependencies: `pip install -r scripts/moonraker-dev-reqs.txt`
- Install speedups (optional performance boosters): `pip install -r scripts/moonraker-speedups.txt`

### Testing
- Run tests: `pytest tests/`
- Run specific test file: `pytest tests/test_server.py`
- Run with Klipper integration: `pytest --klipper-path=/path/to/klipper --klipper-exec=/path/to/klippy`

### Linting
- Pre-commit hooks are available via the `pre-commit` package
- Configure with: `pre-commit install`

### Running
- Entry point: `moonraker` command (calls `moonraker/server:main`)
- Config location: `~/printer_data/config/moonraker.conf`

## Architecture

### Event-Driven Server
Moonraker is built on asyncio with optional uvloop support for high performance. The server (`moonraker/server.py`) is the main orchestrator that manages all components.

### Component System
The codebase uses a hierarchical component loading pattern:

1. **Server Components** (loaded first):
   - `application.py` - HTTP/WebSocket server, API registration, authentication
   - `websockets.py` - JSON-RPC over WebSocket support
   - `klippy_connection.py` - Unix socket communication with Klipper

2. **Core Components** (loaded in dependency order):
   - `database.py` - Key-value SQLite storage
   - `file_manager.py` - G-code file management with metadata
   - `authorization.py` - User management and access control
   - `machine.py` - System information and hardware management
   - `job_queue.py` - Print job scheduling
   - `history.py` - Print job history and analytics
   - `webcam.py` - Camera management for timelapse/streaming
   - `update_manager.py` - Software update management
   - `power.py` - Device power control
   - And more...

3. **Optional Components** - Extensions and custom modules (loaded in parallel)

Each component follows the interface pattern:
- `component_init()` - Synchronous initialization
- `async_component_init()` - Asynchronous initialization
- Event handlers and API registration

### Communication Protocols

1. **Klipper Communication**: Unix Domain Socket with Klipper's simplified RPC protocol
2. **Client Communication**:
   - HTTP API for REST-style requests
   - JSON-RPC 2.0 over WebSockets for real-time communication
   - Unix Socket for local process communication
   - MQTT for IoT/integration scenarios

### Event Notification System
Publisher-subscribe pattern for real-time updates. Components register event handlers and emit notifications to connected clients.

### Configuration System
Hierarchical configuration with:
- Main config: `~/printer_data/config/moonraker.conf`
- Component-specific sections
- Template support with Jinja2 integration
- Runtime hot-reload support

### Key Directories
- `moonraker/` - Main source code
- `moonraker/components/` - Component implementations
- `tests/` - pytest test suite with fixtures
- `scripts/` - Installation and utility scripts

### Optional Speedups
- `msgspec` - Fast JSON parsing (Python 3.8+)
- `uvloop` - High-performance event loop

## Component Initialization Flow

1. Parse command-line arguments and environment variables
2. Initialize logging with rollover support
3. Create data directories if needed
4. Generate UUID for instance identification
5. Instantiate Server with config
6. Load core and optional components (with dependency resolution)
7. Start HTTP server and connect to Klipper
