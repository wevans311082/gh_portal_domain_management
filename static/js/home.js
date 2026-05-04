(function () {
    function initVanta() {
        var hero = document.getElementById("vanta-hero");
        if (!hero) {
            return;
        }

        if (!window.VANTA || !window.VANTA.CLOUDS2) {
            hero.classList.add("hero-fallback-bg");
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
            mouseControls: false,
            touchControls: false,
            gyroControls: false,
            minHeight: 350,
            minWidth: 200,
            scale: 1,
            scaleMobile: 1,
            texturePath: texturePath,
            skyColor: 0x0b1e36,
            cloudColor: 0xe8f6ff,
            cloudShadowColor: 0x23435f,
            sunColor: 0xffd7a8,
            sunGlareColor: 0xffefcf,
            sunlightColor: 0xfff7df,
            speed: 0.38
        });
    }

    document.addEventListener("DOMContentLoaded", initVanta);
})();
