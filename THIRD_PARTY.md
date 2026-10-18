# Third-party components

The server uses FastAPI, Starlette, Uvicorn, aiohttp, cryptography, python-multipart, python-dotenv, and Wasmtime. The web UI uses Vue 3 and Vite. Dependency versions are recorded in `uv.lock` and `frontend/package-lock.json`; consult each upstream package for its license.

The server distribution includes a fixed protocol WASM module from [chuankkk/qq-farm-bot](https://github.com/chuankkk/qq-farm-bot), distributed under the MIT License. Its SHA-256 is:

`748ca5e807a41e26ee81e21d8f71e231c0832c1252bf3f4814ba0cd92d5aedff`

The required license notice is in [`licenses/tsdk-wasm-MIT.txt`](licenses/tsdk-wasm-MIT.txt). The server verifies the hash before using the module.

The plant and item reference catalog in `src/qqfarm/resources/plant_catalog.json` is derived from [linguo2625469/qq-farm-bot](https://github.com/linguo2625469/qq-farm-bot) (source commit recorded in the JSON). The upstream project uses the MIT License; its copyright and license notice are retained in [`licenses/plant-catalog-MIT.txt`](licenses/plant-catalog-MIT.txt).

Fiddler is installed and used separately by the user to capture a SAZ file. The server package does not include Fiddler, QQ, or user authorization data.
