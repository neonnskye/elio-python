import platform

import cv2


class FacialRecognition:
    def __init__(self):
        self.HOST_OS = platform.system()
        self.read_cam()

    def read_cam(self):
        cap = cv2.VideoCapture(3)  # OBS Virtual Camera

        if not cap.isOpened():
            print("Error: Could not open webcam.")
            return

        while True:
            ret, frame = cap.read()

            if not ret:
                print("Error: Can't receive frame.")
                break

            cv2.imshow("Webcam Input", frame)

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break


if __name__ == "__main__":
    FacialRecognition()
