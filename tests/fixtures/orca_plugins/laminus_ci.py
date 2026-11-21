# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "Laminus CI"
# version = "1.0"
# type = "slicing-pipeline"
# ///
import json

import orca


class LaminusCI(orca.slicing.SlicingPipelineCapabilityBase):
    def get_name(self):
        return "Laminus CI"

    def on_load(self):
        runs = json.loads(self.get_config()).get("runs", 0)
        if not self.save_config(json.dumps({"runs": runs + 1})):
            raise RuntimeError("Could not save Laminus CI plugin settings")

    def execute(self, ctx):
        return orca.ExecutionResult.success()


@orca.plugin
class LaminusCIPackage(orca.base):
    def register_capabilities(self):
        orca.register_capability(LaminusCI)
