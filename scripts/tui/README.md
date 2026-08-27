# Console Usage Instructions

Use the bundled launcher; it starts both the Rust TUI and the Python Runtime Host.

## Prerequisites

1. **Ensure vLLM is available** at `http://127.0.0.1:11411/v1`.  
   If needed, start it in another terminal:

   ```bash
   cd /mnt/array/sukaih/Project/onr_agent_slim
   conda activate onr
   bash scripts/vllm/start_vllm.sh
   ```

## Launch the Console

2. **Open a terminal** (at least 100×30) and launch the console:

   ```bash
   cd /mnt/array/sukaih/Project/onr_agent_slim
   ./scripts/tui/start_operator_console.sh
   ```

   > **Note:** The first launch may pause while Cargo compiles the console.

## Starting a Mission

3. When the screen changes from `Connecting` to `Editing`, enter the following prompt:

   ```
   Please patrol the environment and confirm that all the events mentioned in the event report are accounted for.
   ```

4. **Submit the mission:**

   - **Alt+Enter** – open activation review.  
     (Try **Ctrl+Enter** if your terminal does not report Alt+Enter correctly.)
   - **Enter** – confirm and activate the mission. **Press it only once.**
   - Wait for the **Run dashboard**.  
     > A complete live mission can take tens of minutes because it performs actual LLM inference.

## While the Mission Runs

5. **Navigation and inspection:**

   - **Tab** / **Shift+Tab** – switch between *Activities* and *Artifacts*.
   - **j**/**k** or **arrow keys** – select entries.
   - **Enter** – inspect the selected artifact.
   - **Esc** – close the artifact inspector.
   - **c**, then **Enter** – cancel the run but keep the console open.
   - **q** – exit; if the run is still active, it asks to cancel first.

## Configuration Notes

- For the first trial, leave `conf/environment_params.yaml:8` set to `coordinator_driven`.  
  After that, changing it to `environment_driven` lets you exercise the asynchronous environment mode through the same TUI.

- **Avoid** `Ctrl+C` during an active run because it exits immediately; use `q` for the managed shutdown path.
