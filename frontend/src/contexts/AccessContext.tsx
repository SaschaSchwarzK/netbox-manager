import { createContext, useContext } from "react";
import { Role } from "../api/client";

const ROLE_ORDER: Record<Role, number> = { viewer: 0, editor: 1, admin: 2 };

export interface AccessValue {
  role: Role;
  appAdmin: boolean;
  can: (minimum: Role) => boolean;
}

export const AccessContext = createContext<AccessValue>({ role: "admin", appAdmin: true, can: () => true });

export function useAccess(): AccessValue {
  return useContext(AccessContext);
}

export function makeAccessValue(role: Role, appAdmin: boolean): AccessValue {
  return { role, appAdmin, can: (minimum: Role) => ROLE_ORDER[role] >= ROLE_ORDER[minimum] };
}
