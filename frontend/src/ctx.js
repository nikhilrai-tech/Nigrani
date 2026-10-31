import { createContext, useContext } from 'react';

export const AppCtx = createContext({ range: '1h', theme: 'light', me: null, server: '', servers: [], multi: false, setServer: () => {} });
export const useApp = () => useContext(AppCtx);
