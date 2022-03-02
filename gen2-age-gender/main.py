import time
import blobconverter
import cv2
import depthai as dai
import numpy as np
from imutils.video import FPS

def frame_norm(frame, bbox):
    normVals = np.full(len(bbox), frame.shape[0])
    normVals[::2] = frame.shape[1]
    return (np.clip(np.array(bbox), 0, 1) * normVals).astype(int)

def create_pipeline():
    print("Creating pipeline...")
    pipeline = dai.Pipeline()

    print("Creating Color Camera...")
    cam = pipeline.create(dai.node.ColorCamera)
    cam.setPreviewSize(1080, 1080)
    cam.setResolution(dai.ColorCameraProperties.SensorResolution.THE_1080_P)
    cam.setInterleaved(False)
    cam.setBoardSocket(dai.CameraBoardSocket.RGB)
    cam_xout = pipeline.create(dai.node.XLinkOut)
    cam_xout.setStreamName("cam_out")
    cam.preview.link(cam_xout.input)

    # ImageManip that will crop the frame before sending it to the Face detection NN node
    face_det_manip = pipeline.create(dai.node.ImageManip)
    face_det_manip.initialConfig.setResize(300, 300)
    face_det_manip.initialConfig.setFrameType(dai.RawImgFrame.Type.RGB888p)

    monoLeft = pipeline.create(dai.node.MonoCamera)
    monoLeft.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
    monoLeft.setBoardSocket(dai.CameraBoardSocket.LEFT)
    monoRight = pipeline.create(dai.node.MonoCamera)
    monoRight.setResolution(dai.MonoCameraProperties.SensorResolution.THE_400_P)
    monoRight.setBoardSocket(dai.CameraBoardSocket.RIGHT)

    stereo = pipeline.create(dai.node.StereoDepth)
    stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.HIGH_DENSITY)
    monoLeft.out.link(stereo.left)
    monoRight.out.link(stereo.right)

    # NeuralNetwork
    print("Creating Face Detection Neural Network...")
    face_det_nn = pipeline.create(dai.node.MobileNetSpatialDetectionNetwork)
    face_det_nn.setConfidenceThreshold(0.5)
    face_det_nn.setBoundingBoxScaleFactor(0.8)
    face_det_nn.setDepthLowerThreshold(100)
    face_det_nn.setDepthUpperThreshold(5000)
    face_det_nn.setBlobPath(blobconverter.from_zoo(name="face-detection-retail-0004", shaves=6))

    cam.preview.link(face_det_manip.inputImage)
    stereo.depth.link(face_det_nn.inputDepth)

    # Link Face ImageManip -> Face detection NN node
    face_det_manip.out.link(face_det_nn.input)

    # Send face detections to the host (for bounding boxes)
    face_det_xout = pipeline.create(dai.node.XLinkOut)
    face_det_xout.setStreamName("face_det_out")
    face_det_nn.out.link(face_det_xout.input)

    # Script node will take the output from the face detection NN as an input and set ImageManipConfig
    # to the 'age_gender_manip' to crop the initial frame
    image_manip_script = pipeline.create(dai.node.Script)
    face_det_nn.out.link(image_manip_script.inputs['face_det_in'])

    # Only send metadata, we are only interested in timestamp, so we can sync
    # depth frames with NN output
    face_det_nn.passthrough.link(image_manip_script.inputs['passthrough'])

    image_manip_script.setScript("""
l = [] # List of images
# So the correct frame will be the first in the list
# For this experiment this function is redundant, since everything
# runs in blocking mode, so no frames will get lost
def remove_prev_frame(seq):
    for rm, frame in enumerate(l):
        if frame.getSequenceNum() == seq:
            # node.warn(f"List len {len(l)} Frame with same seq num: {rm},seq {seq}")
            break
    for i in range(rm):
        l.pop(0)
def correct_bb(bb):
    if bb.xmin < 0: bb.xmin = 0.0
    if bb.ymin < 0: bb.ymin = 0.0
    if bb.xmax > 1: bb.xmax = 0.999
    if bb.ymax > 1: bb.ymax = 0.999
    return bb
while True:
    preview = node.io['preview'].tryGet()
    if preview is not None:
        # node.warn(f"New frame {preview.getSequenceNum()}")
        l.append(preview)

    face_dets = node.io['face_det_in'].tryGet()
    # node.warn(f"Faces detected: {len(face_dets)}")
    if face_dets is not None:
        passthrough = node.io['passthrough'].get()
        seq = passthrough.getSequenceNum()
        # node.warn(f"New detection {seq}")
        if len(l) == 0:
            continue
        remove_prev_frame(seq)
        img = l[0] # Matching frame is the first in the list
        l.pop(0) # Remove matching frame from the list

        for det in face_dets.detections:
            cfg = ImageManipConfig()
            correct_bb(det)
            cfg.setCropRect(det.xmin, det.ymin, det.xmax, det.ymax)
            cfg.setResize(62, 62)
            cfg.setKeepAspectRatio(False)
            node.io['manip_img'].send(img)
            node.io['manip_cfg'].send(cfg)
""")
    cam.preview.link(image_manip_script.inputs['preview'])

    age_gender_manip = pipeline.create(dai.node.ImageManip)
    age_gender_manip.initialConfig.setResize(62, 62)
    age_gender_manip.setWaitForConfigInput(False)
    image_manip_script.outputs['manip_cfg'].link(age_gender_manip.inputConfig)
    image_manip_script.outputs['manip_img'].link(age_gender_manip.inputImage)

    face_cropped_xout = pipeline.create(dai.node.XLinkOut)
    face_cropped_xout.setStreamName("face_cropped")
    age_gender_manip.out.link(face_cropped_xout.input)

    # Age/Gender second stange NN
    print("Creating Age Gender Neural Network...")
    age_gender_nn = pipeline.create(dai.node.NeuralNetwork)
    age_gender_nn.setBlobPath(blobconverter.from_zoo(name="age-gender-recognition-retail-0013", shaves=6))
    age_gender_manip.out.link(age_gender_nn.input)

    age_gender_nn_xout = pipeline.create(dai.node.XLinkOut)
    age_gender_nn_xout.setStreamName("age_gender_out")
    age_gender_nn.out.link(age_gender_nn_xout.input)

    print("Pipeline created.")
    return pipeline

with dai.Device() as device:
    device.setLogLevel(dai.LogLevel.WARN)
    device.setLogOutputLevel(dai.LogLevel.WARN)

    print("Starting pipeline...")
    device.startPipeline(create_pipeline())
    cam_out = device.getOutputQueue("cam_out", 4, False)
    face_q = device.getOutputQueue("face_det_out", 4, False)
    face_cropped_q = device.getOutputQueue("face_cropped", 4, False)
    age_gender_q = device.getOutputQueue("age_gender_out", 4, False)

    detections = []
    results = []

    fps = FPS()
    fps.start()

    try:
        while True:
            frame = cam_out.get().getCvFrame()
            fps.update()
            debug_frame = frame.copy()

            face_cropped_in = face_cropped_q.tryGet()
            if face_cropped_in is not None:
                cv2.imshow("cropped", face_cropped_in.getCvFrame())

            det_in = face_q.tryGet()
            if det_in is not None:
                detections = det_in.detections
                for detection in detections:
                    bbox = frame_norm(frame, (detection.xmin, detection.ymin, detection.xmax, detection.ymax))

                    # If there is a face detected, there will also be an age/gender
                    # inference result available soon, so we can wait for it
                    det = age_gender_q.get()
                    age = int(float(np.squeeze(np.array(det.getLayerFp16('age_conv3')))) * 100)
                    gender = np.squeeze(np.array(det.getLayerFp16('prob')))
                    gender_str = "female" if gender[0] > gender[1] else "male"

                    while not len(results) < len(detections) and len(results) > 0:
                        results.pop(0)
                    results.append({
                        "bbox": bbox,
                        "3d": detection.spatialCoordinates,
                        "gender": gender_str,
                        "age": age,
                        "ts": time.time()
                    })

            # Dispaly results for 0.2 seconds after the inference
            results = list(filter(lambda result: time.time() - result["ts"] < 0.2, results))

            if frame is not None:
                for result in results:
                    bbox = result["bbox"]
                    cv2.rectangle(debug_frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), (10, 245, 10), 2)
                    y = (bbox[1] + bbox[3]) // 2
                    cv2.putText(debug_frame, str(result["age"]), (bbox[0], y), cv2.FONT_HERSHEY_TRIPLEX, 1.5, (0, 0, 0), 8)
                    cv2.putText(debug_frame, str(result["age"]), (bbox[0], y), cv2.FONT_HERSHEY_TRIPLEX, 1.5, (255, 255, 255), 2)
                    cv2.putText(debug_frame, result["gender"], (bbox[0], y + 30), cv2.FONT_HERSHEY_TRIPLEX, 1.5, (0, 0, 0), 8)
                    cv2.putText(debug_frame, result["gender"], (bbox[0], y + 30), cv2.FONT_HERSHEY_TRIPLEX, 1.5, (255, 255, 255), 2)
                    # You could also get result["3d"].x and result["3d"].y coordinates
                    coords = "Z: {:.2f} m".format(result["3d"].z/1000)
                    cv2.putText(debug_frame, coords, (bbox[0], y + 60), cv2.FONT_HERSHEY_TRIPLEX, 1, (0, 0, 0), 8)
                    cv2.putText(debug_frame, coords, (bbox[0], y + 60), cv2.FONT_HERSHEY_TRIPLEX, 1, (255, 255, 255), 2)

                aspect_ratio = frame.shape[1] / frame.shape[0]
                cv2.imshow("Camera_view", debug_frame)
                if cv2.waitKey(1) == ord('q'):
                    cv2.destroyAllWindows()
                    break
    except KeyboardInterrupt:
        pass

fps.stop()
print("FPS: {:.2f}".format(fps.fps()))
