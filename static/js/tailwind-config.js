window.tailwind = window.tailwind || {};
tailwind.config = {
    theme: {
        extend: {
            colors: {
                brand: {
                    50: "#f0f7ff",
                    100: "#dcecfc",
                    200: "#b4d6fa",
                    300: "#7fb8f5",
                    400: "#4a9aef",
                    500: "#0078d4",
                    600: "#106ebe",
                    700: "#005a9e",
                    800: "#004578",
                    900: "#002050",
                },
                fluent: {
                    canvas: "#f5f5f5",
                    surface: "#ffffff",
                    border: "#e1dfdd",
                    subtle: "#faf9f8",
                    ink: "#242424",
                    muted: "#605e5c",
                    nav: "#201f1e",
                    navHover: "#292827",
                    accent: "#0078d4",
                    accentHover: "#106ebe",
                    success: "#107c10",
                    warning: "#ffb900",
                    danger: "#d13438",
                },
            },
            fontFamily: {
                sans: [
                    "Segoe UI",
                    "Segoe UI Web (West European)",
                    "-apple-system",
                    "BlinkMacSystemFont",
                    "Roboto",
                    "Helvetica Neue",
                    "sans-serif",
                ],
            },
            boxShadow: {
                fluent: "0 0.3px 0.9px rgba(0,0,0,.12), 0 1.6px 3.6px rgba(0,0,0,.12)",
                "fluent-md": "0 1.6px 3.6px rgba(0,0,0,.13), 0 0.3px 0.9px rgba(0,0,0,.11)",
                "fluent-lg": "0 6.4px 14.4px rgba(0,0,0,.13), 0 1.2px 3.6px rgba(0,0,0,.11)",
                drawer: "0 0 2px rgba(0,0,0,.12), 0 8px 16px rgba(0,0,0,.14)",
            },
            borderRadius: {
                fluent: "4px",
                "fluent-lg": "8px",
            },
        },
    },
};
