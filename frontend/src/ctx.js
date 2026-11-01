import { createContext, useContext } from 'react';

export const AppCtx = createContext({ range: '1h', theme: 'light', me: null, can: { edit: false, admin: false }, server: '', servers: [], multi: false, setServer: () => {} });
export const useApp = () => useContext(AppCtx);
