import { createContext, useContext } from "react";
import { Role } from "../api/client";

const ROLE_ORDER: Record<Role, number> = { viewer: 0, editor: 1, admin: 2 };

export interface AccessValue {
  role: Role;
  can: (minimum: Role) => boolean;
}

export const AccessContext = createContext<AccessValue>({ role: "admin", can: () => true });

export function useAccess(): AccessValue {
  return useContext(AccessContext);
}

export function makeAccessValue(role: Role): AccessValue {
  return { role, can: (minimum: Role) => ROLE_ORDER[role] >= ROLE_ORDER[minimum] };
}
