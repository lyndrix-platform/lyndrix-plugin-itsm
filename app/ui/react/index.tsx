import PluginApp from './PluginApp'

// Explicit global assignment — the shell looks for window.__lyndrix_plugin_<safe id>.
;(window as unknown as Record<string, unknown>)['__lyndrix_plugin_lyndrix_plugin_itsm'] = {
  PluginApp,
  pluginRoutes: [
    { path: '/itsm', label: 'Service Management', icon: 'support_agent', sidebar_visible: true },
  ],
}
