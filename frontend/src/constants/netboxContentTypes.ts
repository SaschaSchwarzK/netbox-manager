// A curated (not exhaustive) list of NetBox's most commonly custom-fielded
// content types, grouped the way NetBox's own UI groups them. Exact
// availability varies by NetBox version/plugins, so the picker also accepts
// free-text entries for anything not listed here.
export const CONTENT_TYPE_GROUPS: { group: string; types: { value: string; label: string }[] }[] = [
  {
    group: "DCIM",
    types: [
      { value: "dcim.site", label: "Site" },
      { value: "dcim.location", label: "Location" },
      { value: "dcim.rack", label: "Rack" },
      { value: "dcim.device", label: "Device" },
      { value: "dcim.devicetype", label: "Device type" },
      { value: "dcim.manufacturer", label: "Manufacturer" },
      { value: "dcim.module", label: "Module" },
      { value: "dcim.moduletype", label: "Module type" },
      { value: "dcim.interface", label: "Interface" },
      { value: "dcim.cable", label: "Cable" },
      { value: "dcim.powerfeed", label: "Power feed" },
      { value: "dcim.powerpanel", label: "Power panel" },
      { value: "dcim.virtualchassis", label: "Virtual chassis" },
      { value: "dcim.platform", label: "Platform" },
      { value: "dcim.inventoryitem", label: "Inventory item" },
    ],
  },
  {
    group: "IPAM",
    types: [
      { value: "ipam.ipaddress", label: "IP address" },
      { value: "ipam.prefix", label: "Prefix" },
      { value: "ipam.iprange", label: "IP range" },
      { value: "ipam.aggregate", label: "Aggregate" },
      { value: "ipam.vlan", label: "VLAN" },
      { value: "ipam.vrf", label: "VRF" },
      { value: "ipam.asn", label: "ASN" },
      { value: "ipam.fhrpgroup", label: "FHRP group" },
      { value: "ipam.routetarget", label: "Route target" },
      { value: "ipam.service", label: "Service" },
    ],
  },
  {
    group: "Virtualization",
    types: [
      { value: "virtualization.virtualmachine", label: "Virtual machine" },
      { value: "virtualization.vminterface", label: "VM interface" },
      { value: "virtualization.cluster", label: "Cluster" },
      { value: "virtualization.clustertype", label: "Cluster type" },
    ],
  },
  {
    group: "Tenancy",
    types: [
      { value: "tenancy.tenant", label: "Tenant" },
      { value: "tenancy.contact", label: "Contact" },
    ],
  },
  {
    group: "Circuits",
    types: [
      { value: "circuits.circuit", label: "Circuit" },
      { value: "circuits.provider", label: "Provider" },
    ],
  },
  {
    group: "Wireless / VPN",
    types: [
      { value: "wireless.wirelesslan", label: "Wireless LAN" },
      { value: "vpn.tunnel", label: "Tunnel" },
    ],
  },
];

export const ALL_CONTENT_TYPES = CONTENT_TYPE_GROUPS.flatMap((g) => g.types);
