import javax.swing.filechooser.FileSystemView;

/** Synthetic main: never run actual OCR as a bootstrap regression test. */
public final class Audiveris {
    public static void main(String[] args) {
        System.out.println("QA_TARGET_REACHED=true");
        for (int index = 0; index < args.length; index++) {
            System.out.println("QA_ARG_" + index + "=" + args[index]);
        }
        if (args.length > 0 && args[0].equals("fail")) {
            throw new IllegalArgumentException("QA_ORIGINAL_FAILURE");
        }
        System.out.println("QA_DEFAULT=" + FileSystemView.getFileSystemView().getDefaultDirectory());
        System.out.println("QA_HOME=" + FileSystemView.getFileSystemView().getHomeDirectory());
    }
}
