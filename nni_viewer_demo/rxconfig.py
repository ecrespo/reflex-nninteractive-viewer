import reflex as rx

config = rx.Config(
    app_name="nni_viewer_demo",
    plugins=[rx.plugins.RadixThemesPlugin(theme=rx.theme(appearance="dark", accent_color="blue"))],
    disable_plugins=[rx.plugins.SitemapPlugin],
)
