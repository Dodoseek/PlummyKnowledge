import { authApi } from './authApi';

/**
 * Whether the cached session has a permission. Only for showing or hiding UI: the backend checks permissions
 * on every request and is the security boundary.
 */
export function useHasPermission(permission: string): boolean {
  const { data } = authApi.endpoints.getSession.useQueryState();
  return data?.permissions.includes(permission) ?? false;
}

export const PERMISSIONS = {
  usersRead: 'users.read',
  usersManage: 'users.manage',
  groupsRead: 'groups.read',
  groupsManage: 'groups.manage',
  rolesRead: 'roles.read',
  rolesAssign: 'roles.assign',
  providersRead: 'providers.read',
  providersManage: 'providers.manage',
  knowledgeBasesManage: 'knowledge_bases.manage',
  assistantsManage: 'assistants.manage',
  chatUse: 'chat.use',
} as const;
