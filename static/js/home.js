(function () {
    function initVanta() {
        var hero = document.getElementById("vanta-hero");
        if (!hero || !window.VANTA || !window.VANTA.CLOUDS2) {
            return;
        }

        window.VANTA.CLOUDS2({
            el: "#vanta-hero",
            mouseControls: true,
            touchControls: true,
            gyroControls: false,
            minHeight: 350,
            minWidth: 200,
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
