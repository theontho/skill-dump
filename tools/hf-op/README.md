# hf-op

Run a command with `HF_TOKEN` obtained through the existing `op-session` grant:

```sh
hf-op hf download OWNER/MODEL
hf-op python script.py
```

By default the reference is
`op://Personal/Hugging Face Personal Access Token/user access token`;
`HF_TOKEN_OP_REF` overrides it. The token is passed only through the child
process's environment, never command-line arguments.

If 1Password cannot supply the token, the command uses the existing
`${HF_HOME:-$HOME/.cache/huggingface}/token` from `hf auth login` and reports
the fallback on stderr. Without either token it fails without launching the
command. It never initiates authentication or opens the desktop app.

`HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE` are set to `0`, as in the original
standalone helper. Run `python3 -m unittest -q test_hf_op` here for fake-token tests.
