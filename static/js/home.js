(function () {
    function initVanta() {
        var hero = document.getElementById("vanta-hero");
        if (!hero || !window.VANTA || !window.VANTA.CLOUDS2) {
            return;
        }

        // Vanta Clouds2 needs a noise texture; the bundled default is
        // "./gallery/noise.png" which 404s on our site. We host the texture
        // ourselves and inject the URL via a data attribute on <body>
        // (set in base.html so we don't hard-code STATIC_URL here).
        var texturePath = (document.body && document.body.dataset.noiseTexture) ||
            "/static/images/noise.png";

        window.VANTA.CLOUDS2({
            el: "#vanta-hero",
            mouseControls: true,
            touchControls: true,
            gyroControls: false,
            minHeight: 350,
            minWidth: 200,
            texturePath: texturePath,
            skyColor: 0x0f172a,
            cloudColor: 0xb6e2ff,
            cloudShadowColor: 0x1e293b,
            sunColor: 0x38bdf8,
            sunGlareColor: 0x7dd3fc,
            sunlightColor: 0x0ea5e9,
            speed: 0.8
        });
    }

    document.addEventListener("DOMContentLoaded", initVanta);
})();
