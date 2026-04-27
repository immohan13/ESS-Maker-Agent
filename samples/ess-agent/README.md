# Vendored ESS sample (placeholder)

This folder is reserved for a read-only mirror of the **EmployeeSelfServiceAgent** sample
from <https://github.com/microsoft/CopilotStudioSamples/tree/main/EmployeeSelfServiceAgent>.

Bring it in with:

```pwsh
# from repo root
git clone --depth=1 --filter=blob:none --sparse https://github.com/microsoft/CopilotStudioSamples .tmp-cps
cd .tmp-cps; git sparse-checkout set EmployeeSelfServiceAgent
Copy-Item -Recurse EmployeeSelfServiceAgent\* ..\ess-maker\samples\ess-agent\
cd ..; Remove-Item -Recurse -Force .tmp-cps
```

The ESS Maker tools (`patch_topic_file`, `read_dataverse_descriptions`, `patch_dataverse_text`)
operate against files in this folder when the maker doesn't have a live agent yet.
