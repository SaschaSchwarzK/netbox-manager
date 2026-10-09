import * as yaml from "js-yaml";
import { describe, expect, it } from "vitest";

import { toNetboxImportYaml, updateDeviceTypeField } from "./DeviceTypeEditor";

it("round-trips a field edit without dropping unrelated YAML keys", () => {
  const source = yaml.load(`manufacturer: Acme
model: Router 1
slug: router-1
u_height: 1
interfaces:
  - name: Ethernet1
    type: 1000base-t
comments: original
`) as Record<string, unknown>;
  const edited = updateDeviceTypeField(source, "comments", "updated");
  const roundTrip = yaml.load(yaml.dump(toNetboxImportYaml(edited), { sortKeys: false }));
  expect(roundTrip).toEqual({ ...source, comments: "updated" });
});
