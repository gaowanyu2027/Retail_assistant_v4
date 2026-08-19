"""实际测量摄像头读取帧率"""
import time

import cv2


def main():
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("摄像头打开失败")
        return

    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 60)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    start = time.time()
    count = 0
    while time.time() - start < 5:
        ok, _ = cap.read()
        if ok:
            count += 1

    elapsed = time.time() - start
    fps = count / elapsed if elapsed > 0 else 0
    print(f"实际读取帧数: {count}")
    print(f"实际FPS: {fps:.1f}")
    print(
        f"OpenCV属性FPS: {cap.get(cv2.CAP_PROP_FPS):.1f}, "
        f"分辨率: {cap.get(cv2.CAP_PROP_FRAME_WIDTH):.0f}x"
        f"{cap.get(cv2.CAP_PROP_FRAME_HEIGHT):.0f}"
    )
    cap.release()


if __name__ == "__main__":
    main()
