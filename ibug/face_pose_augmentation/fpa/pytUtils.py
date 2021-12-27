import math
import numpy as np
from copy import deepcopy
from scipy.spatial import Delaunay
from collections import defaultdict
from typing import Dict, Tuple, Sequence

from . import pyFaceFrontalization as pyFF
from . import pyMM3D as pyMM


__all__ = ['precompute_conn_point', 'make_rotation_matrix', 'align_points', 'fit_3d_shape', 'get_euler_angles',
           'fit_model_with_valid_points', 'model_completion_bfm', 'project_shape', 'parse_pose_parameters',
           'z_buffer', 'z_buffer_tri', 'refine_contour_points', 'image_bbox_to_contour',
           'get_valid_internal_triangles', 'adjust_anchors_z', 'adjust_rotated_anchors', 'back_project_shape',
           'calc_barycentric_coordinates']


def precompute_conn_point(tri: np.ndarray, model_completion: Dict) -> Dict:
    trif_stitch = model_completion['trif_stitch']
    trif_backhead = model_completion['trif_backhead']
    tri_full = np.hstack([tri, trif_backhead, trif_stitch])

    stitch_point = np.unique(trif_stitch)

    conn_point_info = {'stitch_point': stitch_point, 'tri_full': tri_full, 'dict': defaultdict(lambda: [])}
    for ind in stitch_point:
        # blur the ith ind
        conn_tri = np.any(tri_full == ind, axis=0)
        conn_tri = tri_full[:, conn_tri]
        conn_point = np.unique(conn_tri)
        conn_point_info['dict'][ind] = conn_point

    return conn_point_info


def make_rotation_matrix(pitch: float, yaw: float, roll: float, zyx_order: bool = True) -> np.ndarray:
    # Make rotation matrix by Euler angles
    rx = np.array([[1.0, 0.0, 0.0],
                   [0.0, math.cos(pitch), -math.sin(pitch)],
                   [0.0, math.sin(pitch), math.cos(pitch)]])
    ry = np.array([[math.cos(yaw), 0.0, math.sin(yaw)],
                   [0.0, 1.0, 0.0],
                   [-math.sin(yaw), 0.0, math.cos(yaw)]])
    rz = np.array([[math.cos(roll), -math.sin(roll), 0.0],
                   [math.sin(roll), math.cos(roll), 0.0],
                   [0.0, 0.0, 1.0]])
    if zyx_order:
        return rz @ ry @ rx
    else:
        return rx @ ry @ rz


def align_points(p1: np.ndarray, p2: np.ndarray) -> Tuple[float, np.ndarray, np.ndarray]:
    d, n = p1.shape

    mu1 = np.mean(p1, axis=1)
    mu2 = np.mean(p2, axis=1)

    p1_0 = p1 - mu1[:, np.newaxis]
    p2_0 = p2 - mu2[:, np.newaxis]
    sigma1 = np.sum(p1_0 ** 2) / n

    k_mat = p2_0.dot(p1_0.T) / n

    # Matlab's svd command returns U, S and V, but numpy.linalg.svd returns U, the diagonal of S, and V'
    u_mat, g_mat, v_mat = np.linalg.svd(k_mat)
    g_mat = np.diag(g_mat)

    s_mat = np.eye(d)
    if np.linalg.det(k_mat) < 0:
        s_mat[d - 1, d - 1] = -1

    rot_mat = u_mat.dot(s_mat).dot(v_mat)
    c = np.trace(g_mat.dot(s_mat)) / sigma1
    t = mu2 - c * rot_mat.dot(mu1)

    return c, rot_mat, t


def fit_3d_shape(pt3d: np.ndarray, f: float, rot_mat: np.ndarray, t: np.ndarray, mu: np.ndarray,
                 w: np.ndarray, sigma: np.ndarray, beta: float) -> np.ndarray:
    m = pt3d.shape[1]
    t3d = t[:, np.newaxis]

    s3d = f * rot_mat.dot(mu)
    w3d = f * rot_mat.dot(w).reshape((3 * m, -1), order='F')

    lhs = w3d.T.dot(w3d) + beta * np.diag(1.0 / (sigma ** 2))
    rhs = w3d.T.dot((pt3d - s3d - t3d).ravel('F'))
    alpha = np.linalg.lstsq(lhs, rhs, rcond=None)[0]

    return alpha


def get_euler_angles(rot_mat: np.ndarray) -> Tuple[float, float, float]:
    theta1 = math.atan2(rot_mat[1, 2], rot_mat[2, 2])
    c2 = (rot_mat[0, 0] ** 2 + rot_mat[0, 1] ** 2) ** 0.5
    theta2 = math.atan2(-rot_mat[0, 2], c2)
    s1 = math.sin(theta1)
    c1 = math.cos(theta1)
    theta3 = math.atan2(s1 * rot_mat[2, 0] - c1 * rot_mat[1, 0], c1 * rot_mat[1, 1] - s1 * rot_mat[2, 1])

    phi, gamma, theta = -theta1, -theta2, -theta3
    return phi, gamma, theta


def fit_model_with_valid_points(pt3d: np.ndarray, model: Dict, valid_ind: np.ndarray) \
        -> Tuple[float, float, float, float, np.ndarray, np.ndarray]:
    mu = model['mu']
    w = model['w']
    sigma = model['sigma']

    keypoints1 = np.vstack([3 * valid_ind, 3 * valid_ind+1, 3 * valid_ind+2])
    keypoints1 = keypoints1.ravel('F')

    alpha = np.zeros(w.shape[1])
    f, rot_mat, t = 1, np.eye(3), np.zeros(3)

    mu_key = mu[keypoints1]
    mu_key_rs = mu_key.reshape((3, -1), order='F')

    w_key = w[keypoints1]
    w_key_rs = w_key.reshape((3, -1), order='F')

    iterations = 5
    for _ in range(iterations):
        # 1. pose estimation
        vertex_key = mu_key + w_key.dot(alpha)
        vertex_key = vertex_key.reshape((3, -1), order='F')
        f, rot_mat, t = align_points(vertex_key, pt3d)

        # 2. shape fitting
        beta = 3000
        alpha = fit_3d_shape(pt3d, f, rot_mat, t, mu_key_rs, w_key_rs, sigma, beta)

    phi, gamma, theta = get_euler_angles(rot_mat)

    return f, phi, gamma, theta, t, alpha


def model_completion_bfm(projected_vertex: np.ndarray, model_fullhead: Dict, model_completion: Dict,
                         conn_point_info: Dict) -> Tuple[np.ndarray, np.ndarray]:
    muf = model_fullhead['mu']
    wf = model_fullhead['w']

    indf_c = model_completion['indf_c']
    indf_c2b = model_completion['indf_c2b']

    projected_vertex_c2b = projected_vertex[:, indf_c2b]

    f, phi, gamma, theta, t, alpha = fit_model_with_valid_points(projected_vertex_c2b, model_fullhead, indf_c)

    vertexf = muf + wf.dot(alpha)
    vertexf = vertexf.reshape((3, -1), order='F')
    projected_vertexf = f * make_rotation_matrix(phi, gamma, theta, False).dot(vertexf) + t[:, np.newaxis]

    projected_vertex_full = np.hstack([projected_vertex, projected_vertexf])
    tri_full = conn_point_info['tri_full']

    # blend
    iterations = 1
    stitch_point = conn_point_info['stitch_point']
    for _ in range(iterations):
        vertex_blend = projected_vertex_full.copy()
        for ind in stitch_point:
            # blur the i_th ind
            conn_point = conn_point_info['dict'][ind]
            vertex_blend[:, ind] = np.mean(projected_vertex_full[:, conn_point], axis=1)
        projected_vertex_full = vertex_blend

    return projected_vertex_full, tri_full


def project_shape(vertex: np.ndarray, f_rot: np.ndarray, tr: np.ndarray, roi_bbox: np.ndarray,
                  std_size: int = 120) -> np.ndarray:
    # transform to image coordinate scale
    vertex = f_rot.dot(vertex) + tr
    vertex[1, :] = std_size + 1 - vertex[1, :]

    sx, sy, ex, ey = roi_bbox
    scale_x = (ex - sx) / std_size
    scale_y = (ey - sy) / std_size
    vertex[0, :] = vertex[0, :] * scale_x + sx
    vertex[1, :] = vertex[1, :] * scale_y + sy

    s = (scale_x + scale_y) / 2
    vertex[2, :] *= s

    return vertex


def parse_pose_parameters(pose_params: np.ndarray) -> Tuple[float, float, float, np.ndarray, float]:
    phi, gamma, theta = pose_params[:3]
    t3d = pose_params[3:6]
    f = pose_params[-1]

    return phi, gamma, theta, t3d, f


def z_buffer(projected_vertex: np.ndarray, tri: np.ndarray, texture: np.ndarray,
             img_src: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    height, width, num_channels = img_src.shape
    num_vertices = projected_vertex.shape[1]
    num_triangles = tri.shape[1]

    return pyMM.ZBuffer(np.ascontiguousarray((projected_vertex - 1).astype(np.float64)),
                        np.ascontiguousarray(tri.astype(np.int32)),
                        np.ascontiguousarray(texture.astype(np.float64)),
                        np.ascontiguousarray(img_src.astype(np.float64)),
                        num_vertices, num_triangles, width, height, num_channels)


def z_buffer_tri(projected_vertex: np.ndarray, tri: np.ndarray, texture_tri: np.ndarray,
                 img_src: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    height, width, num_channels = img_src.shape
    num_vertices = projected_vertex.shape[1]
    num_triangles = tri.shape[1]
    
    return pyMM.ZBufferTri(np.ascontiguousarray((projected_vertex - 1).astype(np.float64)),
                           np.ascontiguousarray(tri.astype(np.int32)),
                           np.ascontiguousarray(texture_tri.astype(np.float64)),
                           np.ascontiguousarray(img_src.astype(np.float64)),
                           num_vertices, num_triangles, width, height, num_channels)


def refine_contour_points(pitch: float, yaw: float, vertex: np.ndarray, isolines: Sequence[np.ndarray],
                          contour_points: np.ndarray, contour_points_to_refine: Sequence[int]) -> np.ndarray:
    projected_vertex = np.dot(make_rotation_matrix(pitch, yaw, 0), vertex)
    contour_points = deepcopy(contour_points)
    for idx in contour_points_to_refine:
        selected = (np.argmin(projected_vertex[0, isolines[idx]]) if yaw <= 0
                    else np.argmax(projected_vertex[0, isolines[idx]]))
        contour_points[idx] = isolines[idx][selected]

    return contour_points


def image_bbox_to_contour(bbox: np.ndarray, wpnum: float) -> Tuple[np.ndarray, int, int]:
    wp_num = wpnum - 2
    
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]    

    hp_num = round(height / width * (wp_num + 2)) - 2
    w_inter = (width - 1) / (wp_num + 1)
    h_inter = (height - 1) / (hp_num + 1)

    # top edge
    start_point = bbox[[0, 1], np.newaxis]
    interval = np.array([w_inter, 0])[:, np.newaxis]
    img_contour = start_point + np.arange(0, 1+wp_num)*interval

    # right edge
    start_point = bbox[[2, 1], np.newaxis]
    interval = np.array([0, h_inter])[:, np.newaxis]
    img_contour = np.hstack([img_contour, start_point + np.arange(0, 1+hp_num)*interval])

    # bottom edge
    start_point = bbox[[2, 3], np.newaxis]
    interval = np.array([-w_inter, 0])[:, np.newaxis]
    img_contour = np.hstack([img_contour, start_point + np.arange(0, 1+wp_num)*interval])

    # left edge
    start_point = bbox[[0, 3], np.newaxis]
    interval = np.array([0, -h_inter])[:, np.newaxis]
    img_contour = np.hstack([img_contour, start_point + np.arange(0, 1+hp_num)*interval])

    return img_contour, int(wp_num), int(hp_num)


def get_valid_internal_triangles(cont_ver: np.ndarray, tri: np.ndarray) -> np.ndarray:
    valid_bin = np.zeros(tri.shape[1], dtype=np.bool)

    for i in range(cont_ver.shape[1]):
        # for each contour point, find its related tri
        tmp_bin = np.any(tri == i, axis=0)
        conn_tri_ind = np.where(tmp_bin)[0]
        conn_tri = tri[:, tmp_bin]
        angle_list = []

        for j in range(conn_tri.shape[1]):
            # for each connected tri, find the angle centered at i         
            other_point = [x for x in conn_tri[:, j] if x != i]

            line1 = cont_ver[:2, [i, other_point[0]]]
            line1 = line1[:, 1] - line1[:, 0]

            line2 = cont_ver[:2, [i, other_point[1]]]
            line2 = line2[:, 1] - line2[:, 0]

            angle_cos = line1.dot(line2) / np.sqrt(line1.dot(line1)) / np.sqrt(line2.dot(line2))
            angle = np.arccos(angle_cos)
            angle_list.append(angle)        

        if sum(angle_list) > (350 / 180 * np.pi):
            # if the sum of angles around the vertex i is 360, it is a concave point
            for j in range(conn_tri.shape[1]):
                # for each connected tri, find the angle centered at i            
                other_point = [x for x in conn_tri[:, j] if x != i]

                # if edge connecting point i is the contour edge, it is a valid triangle
                bin1 = abs(i - other_point[0]) in [1, cont_ver.shape[1] - 1]
                bin2 = abs(i - other_point[1]) in [1, cont_ver.shape[1] - 1]
                if np.all([bin1, bin2]):
                    valid_bin[conn_tri_ind[j]] = True

    return valid_bin


def adjust_anchors_z(contour_all: np.ndarray, contour_all_ref: np.ndarray,
                     adjust_bin: np.ndarray, tri: np.ndarray) -> np.ndarray:
    # Solve the equation Y = AX only for z coordinates
    y_equ = []
    a_equ = []

    # for each outpoint
    adjust_ind = np.where(adjust_bin)[0]
    for pt in adjust_ind:
        # find the corresponding tri
        tmp_bin = np.any(tri == pt, axis=0)

        # find connecting point
        temp = tri[:, tmp_bin]
        connect = np.unique(temp)
        connect = connect[connect != pt]
        for pt_con in connect:
            if adjust_bin[pt_con]:
                # if connected to a point need adjustment, we module their relationships
                z_offset = contour_all_ref[2, pt] - contour_all_ref[2, pt_con]
                pt1 = np.where(adjust_ind == pt)[0]
                pt_con1 = np.where(adjust_ind == pt_con)[0]

                a_equ.append(np.zeros(len(adjust_ind)))
                a_equ[-1][pt1] = 1
                a_equ[-1][pt_con1] = -1
                y_equ.append(z_offset)
            else:
                # if connected to solid point, we module the positions
                z_new = contour_all_ref[2, pt] - contour_all_ref[2, pt_con] + contour_all[2, pt_con]
                pt1 = np.where(adjust_ind == pt)[0]

                a_equ.append(np.zeros(len(adjust_ind)))
                a_equ[-1][pt1] = 1
                y_equ.append(z_new)

    # get the new position
    x_equ = np.linalg.lstsq(np.vstack(a_equ), np.array(y_equ), rcond=None)[0]
    contour_all_z = deepcopy(contour_all)
    contour_all_z[2, adjust_ind] = x_equ.reshape(-1)

    return contour_all_z


def adjust_rotated_anchors(all_vertex_src: np.ndarray, all_vertex_ref: np.ndarray, all_vertex_adjust: np.ndarray,
                           tri: np.ndarray, anchor_flags: np.ndarray) -> np.ndarray:
    # Solve the equation Y = AX for x and y coordinates
    y_equ = []
    a_equ = []

    # for each outpoint
    adjust_ind = np.where(np.any([anchor_flags == 2, anchor_flags == 3], axis=0))[0]
    for pt in adjust_ind:
        # find the corresponding tri
        tmp_bin = np.any(tri == pt, axis=0)

        # find connecting point
        temp = tri[:, tmp_bin]
        connect = np.unique(temp)
        connect = connect[connect != pt]

        # the relationship of [pt, pt_con]
        for pt_con in connect:
            if anchor_flags[pt] == 2:
                # if base point is a src point, prefer src relation
                if anchor_flags[pt_con] == 1:
                    # if connect to a base point, module the positions
                    x_new = all_vertex_src[0, pt] - all_vertex_src[0, pt_con] + all_vertex_adjust[0, pt_con]
                    y_new = all_vertex_src[1, pt] - all_vertex_src[1, pt_con] + all_vertex_adjust[1, pt_con]

                    pt1 = np.where(adjust_ind == pt)[0]

                    a_equ.append(np.zeros(shape=(2, 2 * len(adjust_ind))))
                    a_equ[-1][0, 2 * pt1] = 1
                    a_equ[-1][1, 2 * pt1 + 1] = 1
                    y_equ.extend([x_new, y_new])
                else:  # anchor_flags(pt_con) in [2, 3]
                    # src-src and src-ref relationships: based on src relationship
                    x_offset = all_vertex_src[0, pt] - all_vertex_src[0, pt_con]
                    y_offset = all_vertex_src[1, pt] - all_vertex_src[1, pt_con]

                    pt1 = np.where(adjust_ind == pt)[0]
                    pt_con1 = np.where(adjust_ind == pt_con)[0]

                    a_equ.append(np.zeros(shape=(2, 2 * len(adjust_ind))))
                    a_equ[-1][0, 2 * pt1] = 1
                    a_equ[-1][0, 2 * pt_con1] = -1
                    a_equ[-1][1, 2 * pt1 + 1] = 1
                    a_equ[-1][1, 2 * pt_con1 + 1] = -1
                    y_equ.extend([x_offset, y_offset])
            else:  # anchor_flags(pt) == 3
                # if it is a ref point, prefer ref relation
                if anchor_flags[pt_con] == 1:
                    # if connect to a base point, module the positions
                    x_new = all_vertex_ref[0, pt] - all_vertex_ref[0, pt_con] + all_vertex_adjust[0, pt_con]
                    y_new = all_vertex_ref[1, pt] - all_vertex_ref[1, pt_con] + all_vertex_adjust[1, pt_con]

                    pt1 = np.where(adjust_ind == pt)[0]

                    a_equ.append(np.zeros(shape=(2, 2 * len(adjust_ind))))
                    a_equ[-1][0, 2 * pt1] = 1
                    a_equ[-1][1, 2 * pt1 + 1] = 1
                    y_equ.extend([x_new, y_new])
                else:
                    # ref-ref relationships: based on ref relationship
                    x_offset = all_vertex_ref[0, pt] - all_vertex_ref[0, pt_con]
                    y_offset = all_vertex_ref[1, pt] - all_vertex_ref[1, pt_con]

                    pt1 = np.where(adjust_ind == pt)[0]
                    pt_con1 = np.where(adjust_ind == pt_con)[0]

                    a_equ.append(np.zeros(shape=(2, 2 * len(adjust_ind))))
                    a_equ[-1][0, 2 * pt1] = 1
                    a_equ[-1][0, 2 * pt_con1] = -1
                    a_equ[-1][1, 2 * pt1 + 1] = 1
                    a_equ[-1][1, 2 * pt_con1 + 1] = -1
                    y_equ.extend([x_offset, y_offset])

    # get the new position
    x_equ = np.linalg.lstsq(np.vstack(a_equ), np.array(y_equ), rcond=None)[0]
    all_vertex_adjust[:2, adjust_ind] = x_equ.reshape((2, -1), order='F')
    all_vertex_adjust[2, adjust_ind] = all_vertex_ref[2, adjust_ind]

    return all_vertex_adjust


def back_project_shape(vertex: np.ndarray, f_rot: np.ndarray, tr: np.ndarray, roi_bbox: np.ndarray,
                       std_size: int = 120) -> np.ndarray:
    sx, sy, ex, ey = roi_bbox
    scale_x = (ex - sx) / std_size
    scale_y = (ey - sy) / std_size
    s = (scale_x + scale_y) / 2

    vertex[2, :] /= s
    vertex[0, :] = (vertex[0, :] - sx)/scale_x
    vertex[1, :] = (vertex[1, :] - sy)/scale_y  

    vertex[1, :] = std_size + 1 - vertex[1, :]

    vertex = np.linalg.inv(f_rot).dot((vertex - tr))
    
    return vertex


def ImageMeshing(vertex, tri_plus, vertex_full, tri_full, vertexm_full, ProjectVertex_full, ProjectVertexm_full,
                 fR, T, roi_bbox, f, pitch, yaw, roll, t3d,
                 keypoints, keypointsfull_contour, parallelfull_contour, img, layer_width, eliminate_inner_tri=False):
    # We will mark a set of points to help triangulation the whole image
    # These points are arranged as multiple layers around face contour
    # The layers are set between face contour and bbox
    height, width = img.shape[:2]
    layer = len(layer_width)    
    
    contlist = [np.empty(shape=(3, 0)) for _ in range(layer+2)]
    bboxlist = [np.empty(shape=(0,)) for _ in range(layer+2)]

    # 1. Get the necessary face_contour
    if yaw < 0:
        face_contour_modify = list(range(8)) + list(range(24, 30))
    else:
        face_contour_modify = list(range(9, 23))
    face_contour_ind = refine_contour_points(pitch, yaw, vertex_full, parallelfull_contour,
                                             keypointsfull_contour, face_contour_modify)
    face_contour = ProjectVertex_full[:, face_contour_ind]

    contlist[0] = face_contour
    tl = np.min(face_contour, axis=1)
    br = np.max(face_contour, axis=1)
    bboxlist[0] = np.hstack([tl, br])

    # 2. Get the MultiLayers between face_contour and img_contour
    # other layers
    nosetip = keypoints[33]
    contour_base = face_contour
    face_center = np.mean(contour_base[:2], axis=1)

    for i in range(1,1+layer):
        curlayer_width = 1 + layer_width[i-1]
        contour = face_center[:,np.newaxis] + curlayer_width * (contour_base[:2] - face_center[:,np.newaxis])     
        
        t3d_cur = (1-curlayer_width)*fR.dot(vertex[:,nosetip][:, np.newaxis]) + T

        contour3d = project_shape(vertex_full[:,face_contour_ind], curlayer_width*fR, t3d_cur, roi_bbox)
        
        contour = np.vstack([contour, contour3d[2,:]])

        contlist[i] = contour
        tl = np.min(contour[:2,:], axis=1)
        br = np.max(contour[:2,:], axis=1)
        bboxlist[i] = np.hstack([tl, br])

    # Get the img_contour
    wp_num = 7
    bbox1 = bboxlist[layer]
    bbox2 = bboxlist[layer-1]
    margin = bbox1 - bbox2
    bbox = bbox1 + margin
    bbox[0] = min(bbox[0],1)
    bbox[1] = min(bbox[1],1)
    bbox[2] = max(bbox[2],width)
    bbox[3] = max(bbox[3],height)
    bboxlist[layer+1] = bbox
    wp_num1 = round(wp_num / (bbox1[2]-bbox1[0]) * (bbox[2]-bbox[0]))

    img_contour, wp_num, hp_num = image_bbox_to_contour(bbox, wp_num1)
    contlist[layer+1] = np.vstack([img_contour, np.zeros((1,img_contour.shape[1]))])    

    # Triangulation
    contour_all = np.hstack([item for item in contlist if len(item) > 0])
    tri_all = Delaunay(contour_all.T[:,:2]).simplices.T

    # further judge the internal triangles, since there maybe concave tri
    if eliminate_inner_tri:
        inbin = np.all(tri_all<face_contour.shape[1], axis=0)
        tri_inner = tri_all[:, inbin]
        cont_inner = contlist[0]
        valid_inner_tri = get_valid_internal_triangles(cont_inner, tri_inner)
        tri_inner = tri_all[:, inbin]
        tri_all = np.hstack([tri_all[:,~inbin], tri_inner[:,valid_inner_tri]])

    ## Now we need to determine the z coordinates of each contour point
    # Following the two considerations
    # 1. There always have face regions in the background
    # 2. We don't care about the alignment result of background pixels

    # the z coordinates of img contour out
    img_contour = contlist[-1]
    img_contour_co = range(contour_all.shape[1]-img_contour.shape[1], contour_all.shape[1])

    for i in range(len(img_contour_co)):    
        # find the related triangle
        tmp_bin = np.any(tri_all == img_contour_co[i], axis=0)
        conn_tri = tri_all[:, tmp_bin]    
        conn_point = np.unique(conn_tri)

        conn_face_contour_ind = sorted(list(set(conn_point).difference(set(img_contour_co))))

        if len(conn_face_contour_ind) == 0:
            img_contour[2,i] = np.inf
            continue    

        # get the z coordinates of each connect face contour
        z_coordinates = contour_all[2, conn_face_contour_ind]
        img_contour[2,i] = np.mean(z_coordinates)    

    contlist[-1] = img_contour
    contour_all = np.hstack(contlist)

    # Complement the point with no face contour correspondence
    img_contour_co = np.arange(contour_all.shape[1]-img_contour.shape[1], contour_all.shape[1])

    tmp_bin = np.isinf(img_contour[2,:])
    invalid_co = np.where(tmp_bin==True)[0]

    while len(invalid_co) > 0:   
        valid_co = np.where(~tmp_bin)[0]
        img_contour_co_cur = img_contour_co[valid_co]

        for i in range(len(img_contour_co)):        
            # find the related triangle
            tmp_bin = np.any(tri_all == img_contour_co[i], axis=0)
            conn_tri = tri_all[:, tmp_bin]    
            conn_point = np.unique(conn_tri)

            conn_face_contour_ind = sorted(list(set(conn_point).intersection(set(img_contour_co_cur))))

            if len(conn_face_contour_ind) == 0:
                continue

            # get the z coordinates of each connect face contour
            z_coordinates = contour_all[2, conn_face_contour_ind] 
            img_contour[2,i] = np.mean(z_coordinates)

        contlist[-1] = img_contour
        contour_all = np.hstack(contlist)

        tmp_bin = np.isinf(img_contour[2,:])
        invalid_co = np.where(tmp_bin==True)[0]

    contlist[-1] = img_contour
    contour_all = np.hstack(contlist)

    # Finally refine the anchor depth with real depth
    depth_ref, tri_ind = z_buffer(ProjectVertex_full, tri_full, ProjectVertexm_full[2, :][:, np.newaxis],
                                  np.zeros((height, width, 1)))
    depth_ref = depth_ref.squeeze(axis=-1)
    # # test draw
    # im1 = Image.fromarray(( 255*(depth_ref-np.min(depth_ref))/(np.max(depth_ref)-np.min(depth_ref)) ).astype('uint8'))
    # im2 = Image.fromarray((255*tri_ind/np.max(tri_ind)).astype('uint8'))    

    contour_all_ref = deepcopy(contour_all)
    # contlist_ref = deepcopy(contlist)
    contlist_new = deepcopy(contlist)

    solid_depth_bin_list = [np.zeros(item.shape[1]) for item in contlist]
    solid_depth_bin_list[0] += 1

    for j in list(range(3,14))+list(range(18,30)):
        count = 0        
        for i in range(1,len(contlist_new)-1):        
            ray = contlist_new[i][:,j]
            x, y = np.around(ray[:2]).astype(np.int)
            if np.any([x < 1, x > width, y < 1, y > height]):
                continue
            if tri_ind[y-1, x-1] == -1:
                continue
            count += 1

        if count < 2:
            continue

        for i in range(1,len(contlist_new)-1):
            ray = contlist_new[i][:,j]
            x, y = np.around(ray[:2]).astype(np.int)
            if np.any([x < 1, x > width, y < 1, y > height]):
                continue      
            if tri_ind[y-1, x-1] == -1:
                continue
            contlist_new[i][2,j] = depth_ref[y-1, x-1]
            solid_depth_bin_list[i][j] = 1

    solid_depth_bin = np.hstack(solid_depth_bin_list).astype(np.bool)
    contour_all_new = np.hstack(contlist_new)

    # finally refine non_solid contour
    contour_all_z = adjust_anchors_z(contour_all_new, contour_all_ref, ~solid_depth_bin, tri_all)
    contour_all_new[2,:] = contour_all_z[2,:]

    counter = 0
    for i, item in enumerate(contlist):
        contlist[i] = contour_all_new[:, counter:counter+item.shape[1]]
        counter += item.shape[1]  
    
    return contlist, tri_all, face_contour_ind, wp_num, hp_num


def ImageRotation(contlist_src, bg_tri, vertex, tri, face_contour_ind,
                  isoline_face_contour, Pose_Para_src, Pose_Para_ref, img, 
                  ProjectVertex_ref, fR, T, roi_box):
    _, yaw, _, _, f = parse_pose_parameters(Pose_Para_src)
    pitch_ref, yaw_ref, roll_ref, t3d_ref, _ = parse_pose_parameters(Pose_Para_ref)

    all_vertex = np.hstack(contlist_src)
    all_vertex_src = deepcopy(all_vertex)

    # 1. get the preliminary position on the ref frame    
    all_vertex_ref = back_project_shape(all_vertex, fR, T, roi_box)
    # Go to the reference position
    R_ref = make_rotation_matrix(pitch_ref, yaw_ref, roll_ref)
    all_vertex_ref = project_shape(all_vertex_ref, f*R_ref, t3d_ref[:,np.newaxis], roi_box)
    
    # 2. Landmark marching 
    if yaw_ref < 0:
        face_contour_modify = list(range(8)) + list(range(24, 30))
    else:
        face_contour_modify = list(range(9, 23))

    adjust_ind = list(range(3, 14)) + list(range(18, 30))
    yaw_base = min(yaw, 0) if yaw_ref < 0 else max(0, yaw)
    yaw_delta = yaw_ref - yaw_base
    yaw_temp = yaw_base + yaw_delta / 2.5

    face_contour_ind = refine_contour_points(pitch_ref, yaw_temp, vertex, isoline_face_contour,
                                             face_contour_ind, face_contour_modify)
    face_contour_ind2 = refine_contour_points(pitch_ref, yaw_base, vertex, isoline_face_contour,
                                              face_contour_ind, face_contour_modify)
    face_contour_ind[adjust_ind] = face_contour_ind2[adjust_ind]
    face_contour_ref = ProjectVertex_ref[:, face_contour_ind]
    all_vertex_adjust = np.zeros(all_vertex_ref.shape)
    all_vertex_adjust[:, :face_contour_ref.shape[1]] = face_contour_ref

    # 5. Rotate other img contour
    # favor relationships on src
    src_seq = deepcopy(face_contour_modify)
    # face relationships on ref
    ref_seq = sorted(list(set(range(len(face_contour_ind))).difference(set(src_seq))))
    # 1 for solid anchor; 2 for src anchor; 3 for ref anchor
    anchor_flags = []

    for i in range(len(contlist_src)):
        flags = np.zeros(contlist_src[i].shape[1])
        if i == 0:
            # the face contour, all are solid anchors
            flags[:] = 1
        elif i == len(contlist_src)-1:
            # the image contour, all are src anchors
            flags[:] = 2
        else:
            # middle points
            flags[src_seq] = 2
            flags[ref_seq] = 3

        anchor_flags.append(flags)
    anchor_flags = np.hstack(anchor_flags)

    all_vertex_adjust = adjust_rotated_anchors(
        all_vertex_src, all_vertex_ref, all_vertex_adjust, bg_tri, anchor_flags)

    counter = 0
    contlist_ref = []
    for i, item in enumerate(contlist_src):
        contlist_ref.append(all_vertex_adjust[:, counter:counter+item.shape[1]])
        counter += item.shape[1]  

    return contlist_ref, t3d_ref


def FaceFrontalizationMappingNosym(tri_ind, all_vertex_src, all_vertex_ref, all_tri):
    height, width = tri_ind.shape[:2]
    all_ver_dim, all_ver_length = all_vertex_src.shape
    all_tri_dim, all_tri_length = all_tri.shape
    all_vertex_src = all_vertex_src - 1
    all_vertex_ref = all_vertex_ref - 1

    return pyFF.pyFaceFrontalizationMappingNosym(
        np.ascontiguousarray(tri_ind.astype(np.int32)), width, height,
        np.ascontiguousarray(all_vertex_src.astype(np.float64).T),
        np.ascontiguousarray(all_vertex_ref.astype(np.float64).T), all_ver_dim, all_ver_length,
        np.ascontiguousarray(all_tri.astype(np.int32).T), all_tri_dim, all_tri_length)


def FaceFrontalizationFilling(img, corres_map):
    height, width, num_channels = img.shape
    return pyFF.pyFaceFrontalizationFilling(np.ascontiguousarray(img.astype(np.float64)),
                                            width, height, num_channels,
                                            np.ascontiguousarray(corres_map.astype(np.float64)))


def calc_barycentric_coordinates(pt, vertices, tri_list):
    a = vertices[tri_list[:, 0]]
    b = vertices[tri_list[:, 1]]
    c = vertices[tri_list[:, 2]]
    v0, v1 = b - a, c - a
    v2 = np.expand_dims(pt, axis=0).repeat(a.shape[0], axis=0) - a
    d00 = (v0 * v0).sum(axis=1)
    d01 = (v0 * v1).sum(axis=1)
    d11 = (v1 * v1).sum(axis=1)
    d20 = (v2 * v0).sum(axis=1)
    d21 = (v2 * v1).sum(axis=1)
    denom = d00 * d11 - d01 * d01
    v = (d11 * d20 - d01 * d21) / denom
    w = (d00 * d21 - d01 * d20) / denom
    u = 1.0 - v - w
    return np.vstack((u, v, w)).T
