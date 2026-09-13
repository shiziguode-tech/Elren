// Read only the real third-party path policy; never invoke Audiveris.main.
public final class AudiverisStateProbe {
    public static void main(String[] args) throws Exception {
        org.elren.omr.ElrenAudiverisBootstrap.configureTaskDirectories();
        Class<?> locations = Class.forName("org.audiveris.omr.WellKnowns");
        for (String field : new String[] {"CONFIG_FOLDER", "DATA_FOLDER", "LOG_FOLDER", "TEMP_FOLDER"}) {
            System.out.println("QA_" + field + "=" + locations.getField(field).get(null));
        }
        System.out.println("QA_USER_HOME=" + System.getProperty("user.home"));
        System.out.println("QA_JAVA_TEMP=" + System.getProperty("java.io.tmpdir"));
        System.out.println("QA_JAVACPP_CACHE=" + System.getProperty("org.bytedeco.javacpp.cachedir"));
        Class<?> ocrClass = Class.forName("org.audiveris.omr.text.tesseract.TesseractOCR");
        Object ocr = ocrClass.getMethod("getInstance").invoke(null);
        System.out.println("QA_OCR_FOLDER=" + ocrClass.getMethod("getOcrFolder").invoke(ocr));
    }
}
