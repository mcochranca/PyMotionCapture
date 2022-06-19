import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Dict, Union

import cv2
import numpy as np
import mediapipe as mp
from rich.progress import Progress, track

from jon_scratch.opencv_camera import TweakedModel
from src.cameras.capture.dataclasses.frame_payload import FramePayload
from src.cameras.persistence.video_writer.video_recorder import VideoRecorder
from src.config.home_dir import get_session_folder_path, get_synchronized_videos_folder_path, \
    get_session_output_data_folder_path
from src.core_processor.mediapipe_skeleton_detector.medaipipe_tracked_points_names_dict import \
    mediapipe_tracked_point_names_dict

logger = logging.getLogger(__name__)


@dataclass
class Mediapipe2dSingleCameraNpyArrays:
    body2d_frameNumber_trackedPointNumber_XY: np.ndarray = None
    rightHand2d_frameNumber_trackedPointNumber_XY: np.ndarray = None
    leftHand2d_frameNumber_trackedPointNumber_XY: np.ndarray = None
    face2d_frameNumber_trackedPointNumber_XY: np.ndarray = None

    body2d_frameNumber_trackedPointNumber_confidence: np.ndarray = None

    @property
    def all_data2d_nFrames_nTrackedPts_XY(self):
        """dimensions will be [number_of_frames , number_of_markers, XY]"""
        return np.hstack([self.body2d_frameNumber_trackedPointNumber_XY,
                          self.rightHand2d_frameNumber_trackedPointNumber_XY,
                          self.leftHand2d_frameNumber_trackedPointNumber_XY,
                          self.face2d_frameNumber_trackedPointNumber_XY])


class MediaPipeSkeletonDetector:
    def __init__(self, session_id: str = None):
        self._session_id = session_id
        self.model_complexity = 2  # can be 0,1, or 2 - higher numbers  are more accurate but heavier computationally
        self.min_detection_confidence = .5
        self.min_tracking_confidence = .5

        self._mediapipe_payload_list = []

        self._mp_drawing = mp.solutions.drawing_utils
        self._mp_drawing_styles = mp.solutions.drawing_styles
        self._mp_holistic = mp.solutions.holistic

        self._body_drawing_spec = self._mp_drawing.DrawingSpec(thickness=1, circle_radius=1)
        self._hand_drawing_spec = self._mp_drawing.DrawingSpec(thickness=1, circle_radius=1)
        self._face_drawing_spec = self._mp_drawing.DrawingSpec(thickness=1, circle_radius=1)

        self._holistic_tracker = self._mp_holistic.Holistic(model_complexity=self.model_complexity,
                                                            min_detection_confidence=self.min_detection_confidence,
                                                            min_tracking_confidence=self.min_tracking_confidence)
        self._mediapipe_tracked_point_names_dict = mediapipe_tracked_point_names_dict

    def detect_skeleton_in_image(self, raw_image, annotate_image=True):
        mediapipe_results = self._holistic_tracker.process(raw_image)  # <-this is where the magic happens
        return mediapipe_results

    def process_session_folder(self,
                               save_annotated_videos: bool = True):
        synchronized_videos_path = Path(get_synchronized_videos_folder_path(self._session_id))
        logger.info(f"loading synchronized videos from: {synchronized_videos_path}")
        each_video_frame_width_list = []
        each_video_frame_height_list = []

        mediapipe2d_single_camera_npy_arrays_list = []
        for video_number, this_synchronized_video_file_path in enumerate(synchronized_videos_path.glob('*.mp4')):
            logger.info(f"Running `mediapipe` skeleton detection on  video: {str(this_synchronized_video_file_path)}")
            this_video_capture_object = cv2.VideoCapture(str(this_synchronized_video_file_path))

            this_video_width = this_video_capture_object.get(cv2.CAP_PROP_FRAME_WIDTH)
            this_video_height = this_video_capture_object.get(cv2.CAP_PROP_FRAME_HEIGHT)


            this_video_mediapipe_results_list = []
            this_video_annotated_images_list = []

            success, image = this_video_capture_object.read()
            if not success or image is None:
                logger.error(f"Failed to load an image from: {str(this_synchronized_video_file_path)}")
                raise Exception

            while success and image is not None:
                mediapipe_results = self.detect_skeleton_in_image(image, annotate_image=True)
                this_video_mediapipe_results_list.append(mediapipe_results)
                annotated_image = self._annotate_image(image, mediapipe_results)
                this_video_annotated_images_list.append(annotated_image)

                success, image = this_video_capture_object.read()

            if save_annotated_videos:
                self.save_annotated_videos(this_video_annotated_images_list,
                                           this_synchronized_video_file_path.stem,
                                           this_video_width,
                                           this_video_height,
                                           )

            this_camera_mediapipe_2d_single_camera_npy_arrays = self._list_of_mediapipe_results_to_npy_arrays(
                this_video_mediapipe_results_list,
                image_width=this_video_width,
                image_height=this_video_height)

            mediapipe2d_single_camera_npy_arrays_list.append(this_camera_mediapipe_2d_single_camera_npy_arrays)

        all_cameras_data2d_list = [m2d.all_data2d_nFrames_nTrackedPts_XY for m2d in
                                   mediapipe2d_single_camera_npy_arrays_list]

        number_of_cameras = len(all_cameras_data2d_list)
        number_of_frames = all_cameras_data2d_list[0].shape[0]
        number_of_tracked_points = all_cameras_data2d_list[0].shape[1]
        number_of_spatial_dimensions = all_cameras_data2d_list[0].shape[2]  # XY, 2d data

        if not number_of_spatial_dimensions == 2:
            logger.error(f"this should be 2D data (XY pixel coordinates), but we founds {number_of_spatial_dimensions}")
            raise Exception


        data2d_numCams_numFrames_numTrackedPts_XY = np.empty((number_of_cameras,
                                                              number_of_frames,
                                                              number_of_tracked_points,
                                                              number_of_spatial_dimensions))

        for this_cam_num in range(number_of_cameras):
            data2d_numCams_numFrames_numTrackedPts_XY[this_cam_num, :, :, :] = all_cameras_data2d_list[this_cam_num]

        self._save_mediapipe2d_data_to_npy(data2d_numCams_numFrames_numTrackedPts_XY)
        return data2d_numCams_numFrames_numTrackedPts_XY


    def _save_mediapipe2d_data_to_npy(self, data2d_numCams_numFrames_numTrackedPts_XY):
        output_data_folder = Path(get_session_output_data_folder_path(self._session_id))
        mediapipe_2dData_save_path = output_data_folder / "mediapipe_2dData_numCams_numFrames_numTrackedPoints_pixelXY.npy"
        logger.info(f"saving: {mediapipe_2dData_save_path}")
        np.save(str(mediapipe_2dData_save_path), data2d_numCams_numFrames_numTrackedPts_XY)



    def save_annotated_videos(self,
                              annotated_images_list: List[np.ndarray],
                              video_file_name: str,
                              image_width: Union[int, float],
                              image_height: Union[int, float],
                              ):

        this_video_name = video_file_name + "_mediapipe.mp4"

        video_recorder = VideoRecorder(this_video_name,
                                       image_width,
                                       image_height,
                                       self._session_id,
                                       mediapipe_annotated_video_bool=True)

        logger.info(f'Saving mediapipe annotated video: {video_recorder.path_to_save_video_file}')
        video_recorder.save_image_list_to_disk(annotated_images_list, frame_rate=30)

    def _annotate_image(self, image, mediapipe_results):
        self._mp_drawing.draw_landmarks(image=image,
                                        landmark_list=mediapipe_results.face_landmarks,
                                        connections=self._mp_holistic.FACEMESH_CONTOURS,
                                        landmark_drawing_spec=None,
                                        connection_drawing_spec=self._mp_drawing_styles
                                        .get_default_face_mesh_contours_style())
        self._mp_drawing.draw_landmarks(image=image,
                                        landmark_list=mediapipe_results.face_landmarks,
                                        connections=self._mp_holistic.FACEMESH_TESSELATION,
                                        landmark_drawing_spec=None,
                                        connection_drawing_spec=self._mp_drawing_styles
                                        .get_default_face_mesh_tesselation_style())
        self._mp_drawing.draw_landmarks(image=image,
                                        landmark_list=mediapipe_results.pose_landmarks,
                                        connections=self._mp_holistic.POSE_CONNECTIONS,
                                        landmark_drawing_spec=self._mp_drawing_styles
                                        .get_default_pose_landmarks_style())
        self._mp_drawing.draw_landmarks(
            image=image,
            landmark_list=mediapipe_results.left_hand_landmarks,
            connections=self._mp_holistic.HAND_CONNECTIONS,
            landmark_drawing_spec=None,
            connection_drawing_spec=self._mp_drawing_styles
                .get_default_hand_connections_style())

        self._mp_drawing.draw_landmarks(
            image=image,
            landmark_list=mediapipe_results.right_hand_landmarks,
            connections=self._mp_holistic.HAND_CONNECTIONS,
            landmark_drawing_spec=None,
            connection_drawing_spec=self._mp_drawing_styles
                .get_default_hand_connections_style())
        return image

    def _list_of_mediapipe_results_to_npy_arrays(self,
                                                 mediapipe_results_list: List,
                                                 image_width: int = 1,
                                                 image_height: int = 1
                                                 ) -> Mediapipe2dSingleCameraNpyArrays:

        body_names_list = self._mediapipe_tracked_point_names_dict["body"]
        right_hand_names_list = self._mediapipe_tracked_point_names_dict["right_hand"]
        left_hand_names_list = self._mediapipe_tracked_point_names_dict["left_hand"]
        face_names_list = self._mediapipe_tracked_point_names_dict["face"]

        # apparently `mediapipe_results.pose_landmarks.landmark` returns something iterable ¯\_(ツ)_/¯
        mediapipe_pose_landmark_iterator = mp.python.solutions.pose.PoseLandmark
        mediapipe_hand_landmark_iterator = mp.python.solutions.hands.HandLandmark
        # TODO - build a better iterator and list of `face_marker_names` that will only pull out the face_counters & iris edges (mp.python.solutions.face_mesh_connections.FACEMESH_CONTOURS, FACE_MESH_IRISES)

        number_of_frames = len(mediapipe_results_list)

        number_of_body_trackedPoints = len(body_names_list)
        number_of_right_hand_trackedPoints = len(right_hand_names_list)
        number_of_left_hand_trackedPoints = len(left_hand_names_list)
        number_of_face_trackedPoints = mp.python.solutions.face_mesh.FACEMESH_NUM_LANDMARKS_WITH_IRISES
        number_of_spatial_dimensions = 2  # this will be 2d XY pixel data

        body2d_frameNumber_trackedPointNumber_XY = np.zeros(
            (number_of_frames, number_of_body_trackedPoints, number_of_spatial_dimensions))
        body2d_frameNumber_trackedPointNumber_XY[:] = np.nan

        body2d_frameNumber_trackedPointNumber_confidence = np.zeros((number_of_frames, number_of_body_trackedPoints))
        body2d_frameNumber_trackedPointNumber_confidence[:] = np.nan  # only body markers get a 'confidence' value

        rightHand2d_frameNumber_trackedPointNumber_XY = np.zeros((number_of_frames,
                                                                  number_of_right_hand_trackedPoints,
                                                                  number_of_spatial_dimensions))
        rightHand2d_frameNumber_trackedPointNumber_XY[:] = np.nan

        leftHand2d_frameNumber_trackedPointNumber_XY = np.zeros((number_of_frames,
                                                                 number_of_left_hand_trackedPoints,
                                                                 number_of_spatial_dimensions))
        leftHand2d_frameNumber_trackedPointNumber_XY[:] = np.nan

        face2d_frameNumber_trackedPointNumber_XY = np.zeros((number_of_frames,
                                                             number_of_face_trackedPoints,
                                                             number_of_spatial_dimensions))
        face2d_frameNumber_trackedPointNumber_XY[:] = np.nan

        all_body_tracked_points_visible_on_this_frame_bool_list = []
        all_right_hand_points_visible_on_this_frame_bool_list = []
        all_left_hand_points_visible_on_this_frame_bool_list = []
        all_face_points_visible_on_this_frame_bool_list = []
        all_tracked_points_visible_on_this_frame_list = []

        for this_frame_number, this_frame_results in enumerate(mediapipe_results_list):

            # get the Body data (aka 'pose')
            if this_frame_results.pose_landmarks is not None:

                for this_landmark_data in this_frame_results.pose_landmarks.landmark:
                    body2d_frameNumber_trackedPointNumber_XY[this_frame_number, :,
                    0] = this_landmark_data.x * image_width
                    body2d_frameNumber_trackedPointNumber_XY[this_frame_number, :,
                    1] = this_landmark_data.y * image_height
                    body2d_frameNumber_trackedPointNumber_confidence[this_frame_number,
                    :] = this_landmark_data.visibility  # mediapipe calls their 'confidence' score 'visibility'

            # get Right Hand data
            if this_frame_results.right_hand_landmarks is not None:
                for this_landmark_data in this_frame_results.right_hand_landmarks.landmark:
                    rightHand2d_frameNumber_trackedPointNumber_XY[this_frame_number, :,
                    0] = this_landmark_data.x * image_width
                    rightHand2d_frameNumber_trackedPointNumber_XY[this_frame_number, :,
                    1] = this_landmark_data.y * image_height

            # get Left Hand data
            if this_frame_results.left_hand_landmarks is not None:
                for this_landmark_data in this_frame_results.left_hand_landmarks.landmark:
                    leftHand2d_frameNumber_trackedPointNumber_XY[this_frame_number, :,
                    0] = this_landmark_data.x * image_width
                    leftHand2d_frameNumber_trackedPointNumber_XY[this_frame_number, :,
                    1] = this_landmark_data.y * image_height

            # get Face data
            if this_frame_results.face_landmarks is not None:
                for this_landmark_data in this_frame_results.face_landmarks.landmark:
                    face2d_frameNumber_trackedPointNumber_XY[this_frame_number, :,
                    0] = this_landmark_data.x * image_width
                    face2d_frameNumber_trackedPointNumber_XY[this_frame_number, :,
                    1] = this_landmark_data.y * image_height

            # check if all tracked points are visible on this frame
            all_body_visible = all(sum(
                np.isnan(body2d_frameNumber_trackedPointNumber_XY[this_frame_number, :, :])) == 0)
            all_body_tracked_points_visible_on_this_frame_bool_list.append(all_body_visible)

            all_right_hand_visible = all(sum(
                np.isnan(rightHand2d_frameNumber_trackedPointNumber_XY[this_frame_number, :, :])) == 0)
            all_right_hand_points_visible_on_this_frame_bool_list.append(all_right_hand_visible)

            all_left_hand_visible = all(sum(
                np.isnan(leftHand2d_frameNumber_trackedPointNumber_XY[this_frame_number, :, :])) == 0)
            all_left_hand_points_visible_on_this_frame_bool_list.append(all_left_hand_visible)

            all_face_visible = all(sum(
                np.isnan(face2d_frameNumber_trackedPointNumber_XY[this_frame_number, :, :])) == 0)
            all_face_points_visible_on_this_frame_bool_list.append(all_face_visible)

            all_points_visible = all([all_body_visible,
                                      all_right_hand_visible,
                                      all_left_hand_visible,
                                      all_face_visible],
                                     )

            all_tracked_points_visible_on_this_frame_list.append(all_points_visible)

        return Mediapipe2dSingleCameraNpyArrays(
            body2d_frameNumber_trackedPointNumber_XY=body2d_frameNumber_trackedPointNumber_XY,
            rightHand2d_frameNumber_trackedPointNumber_XY=rightHand2d_frameNumber_trackedPointNumber_XY,
            leftHand2d_frameNumber_trackedPointNumber_XY=leftHand2d_frameNumber_trackedPointNumber_XY,
            face2d_frameNumber_trackedPointNumber_XY=face2d_frameNumber_trackedPointNumber_XY,
            body2d_frameNumber_trackedPointNumber_confidence=body2d_frameNumber_trackedPointNumber_confidence)
