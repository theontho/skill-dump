# This workstation (lin-omarchy)

- Two VMs already oversubscribe this computer's CPU and RAM; use `agent-coord` for heavy work.
- **Active work** (alignment, sorting, calling, VMs, temp files) **goes on the SSDs** (`/home`, `/mnt/Windows`). The ZFS HDD pool (`/mnt/nas`) holds reference archives only: read from it, don't work or write there. `agent-coord run` refuses outputs on it.
