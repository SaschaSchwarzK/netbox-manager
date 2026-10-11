# NetBox Owner Permission Matrix (JSON Constraints)

## 1. DCIM (Physical Infrastructure)

### Base Object (Owner)
* **Devices**
  * `dcim.device`
  * Constraint:
    ```json
    { "owner__users": "$user" }
    ```

### Components Directly Attached to Devices
Applies to all physical interfaces, bays, and ports of a device.

* **Interfaces:** `dcim.interface`
* **Console Ports:** `dcim.consoleport`
* **Console Server Ports:** `dcim.consoleserverport`
* **Power Ports:** `dcim.powerport`
* **Power Outlets:** `dcim.poweroutlet`
* **Module Bays:** `dcim.modulebay`
* **Device Bays:** `dcim.devicebay`
* **Front Ports:** `dcim.frontport`
* **Rear Ports:** `dcim.rearport`
* **Modules:** `dcim.module`

*Unified constraint for all components above:*

```json
{ "device__owner__users": "$user" }
```

### Inventory & Extensions
* **Inventory Items:** `dcim.inventoryitem`

```json
{ "device__owner__users": "$user" }
```

* **Virtual Device Contexts (VDCs):** `dcim.virtualdevicecontext`

```json
{ "device__owner__users": "$user" }
```

### Cables
* **Cables:** `dcim.cable`

  *(Checks whether the user owns Device A OR Device B)*

```json
{
  "any": [
    { "_termination_a_device__owner__users": "$user" },
    { "_termination_b_device__owner__users": "$user" }
  ]
}
```

### Power Infrastructure & Racks
* **Power Feeds:** `dcim.powerfeed`

```json
{ "rack__devices__owner__users": "$user" }
```

* **Racks:** `dcim.rack`

```json
{ "devices__owner__users": "$user" }
```

* **Rack Reservations:** `dcim.rackreservation`

```json
{ "rack__devices__owner__users": "$user" }
```

* **Power Panels:** `dcim.powerpanel`
* **Cable Bundles:** `dcim.cablebundle`
* **Sites:** `dcim.site`
* **Regions:** `dcim.region`
* **Site Groups:** `dcim.sitegroup`
* **Locations:** `dcim.location`
* **Device Types:** `dcim.devicetype`
* **Module Types:** `dcim.moduletype`
* **Rack Types:** `dcim.racktype`
* **Manufacturers:** `dcim.manufacturer`
* **Platforms:** `dcim.platform`
* **Virtual Chassis:** `dcim.virtualchassis`

*Recommendation:* Grant global read-only access (`view`) without JSON constraints for these global reference objects (structures, types, locations), so users can select them when managing their devices.

---

## 2. Virtualization (Virtual Infrastructure)

### Base Object (Owner)
* **Virtual Machines (VMs):** `virtualization.virtualmachine`

```json
{ "owner__users": "$user" }
```

### VM Components
* **VM Interfaces:** `virtualization.vminterface`

```json
{ "virtual_machine__owner__users": "$user" }
```

* **Virtual Disks:** `virtualization.virtualdisk`

```json
{ "virtual_machine__owner__users": "$user" }
```

### Cluster Structures
* **Clusters:** `virtualization.cluster`
* **Cluster Types:** `virtualization.clustertype`
* **Cluster Groups:** `virtualization.clustergroup`
* **VM Types:** `virtualization.virtualmachinetype`

*Recommendation:* Global read-only access (`view`) without JSON constraints.

---

## 3. IPAM (IP Address Management & Networking)

### Addressing & Services
Since IPAM objects can be linked through generic relations to either Devices or Virtual Machines, the `any` construct covers both paths.

* **IP Addresses:** `ipam.ipaddress`

```json
{
  "any": [
    { "assigned_object_id__device__owner__users": "$user" },
    { "assigned_object_id__virtual_machine__owner__users": "$user" }
  ]
}
```

* **Services:** `ipam.service`

```json
{
  "any": [
    { "device__owner__users": "$user" },
    { "virtual_machine__owner__users": "$user" }
  ]
}
```

* **FHRP Group Assignments:** `ipam.fhrpgroupassignment`

```json
{
  "any": [
    { "interface__device__owner__users": "$user" },
    { "vminterface__virtual_machine__owner__users": "$user" }
  ]
}
```

### Global Network Structures
* **Prefixes (Subnets):** `ipam.prefix`
* **IP Ranges:** `ipam.iprange`
* **Aggregates:** `ipam.aggregate`
* **VLANs:** `ipam.vlan`
* **VRFs:** `ipam.vrf`
* **FHRP Groups:** `ipam.fhrpgroup`
* **ASNs:** `ipam.asn`
* **Roles / Service Templates / VLAN Groups / Route Targets**

*Recommendation:* If these IPAM objects are not segregated per tenant (see Tenancy below), they should be globally readable (`view`) so users can correctly create IP addresses within permitted subnets.

---

## 4. Tenancy & Organizational Objects

If you want users to gain access to objects based on their **Tenant** instead of the device owner, use the following logic:

* **Tenants:** `tenancy.tenant`
* **Contacts:** `tenancy.contact`
* **Contact Assignments:** `tenancy.contactassignment`

*Alternative tenant constraint (if used):*

```json
{ "tenant__users": "$user" }
```

*(Note: Requires the user to be assigned to the tenant object.)*

---

## 5. Circuits (Connectivity & Providers)

* **Circuits:** `circuits.circuit`

```json
{ "terminations__site__devices__owner__users": "$user" }
```

* **Providers / Provider Accounts / Provider Networks / Circuit Types**

*Recommendation:* Global read-only access (`view`) without JSON constraints.

---

## 6. VPN & Wireless

* **Tunnels:** `vpn.tunnel`
* **Tunnel Terminations:** `vpn.tunneltermination`

```json
{
  "any": [
    { "termination_id__device__owner__users": "$user" },
    { "termination_id__virtual_machine__owner__users": "$user" }
  ]
}
```

* **Wireless LANs:** `wireless.wirelesslan`
* **Wireless Links:** `wireless.wirelesslink`
* **IKE/IPsec Policies and Profiles:** Global read-only access (`view`).
