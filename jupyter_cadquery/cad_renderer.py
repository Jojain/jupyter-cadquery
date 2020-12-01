#
# Copyright 2019 Bernhard Walter
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import logging
from multiprocessing import Pool
import numpy as np
import os
from tempfile import NamedTemporaryFile
import time
from uuid import uuid4
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from pythreejs import (
        BufferAttribute,
        BufferGeometry,
        Mesh,
        LineSegmentsGeometry,
        LineMaterial,
        LineSegments2,
        Points,
        PointsMaterial,
        Group,
    )

from .cad_helpers import CustomMaterial
from .ocp_utils import (
    is_vertex,
    is_edge,
    discretize_edge,
    get_edges,
    get_point,
    tessellate,
    tq,
    serialize,
    deserialize,
)
from .utils import (
    explode,
    flatten,
    Color,
    tree_find_single_selector,
)

HASH_CODE_MAX = 2147483647


class _tessellate_wrapper:
    def __init__(self, quality=0.1, angular_tolerance=0.1):
        self.quality = quality
        self.angular_tolerance = angular_tolerance

    def __call__(self, path_hash):
        s = time.time()

        path, hash = path_hash
        shape = deserialize(path)
        try:
            os.unlink(path)
        except:
            logging.warn("Cannot unlink %s", path)

        if RENDER_CACHE.get(hash) is None:
            logging.debug(" Tessellate object(hash=%d)", hash)
            result = (hash, tessellate(shape, self.quality, self.angular_tolerance))
            print("T", (time.time() - s) * 1000)
            return result
        else:
            logging.debug(" Get object(hash=%d) from cache", hash)
            return None


class RenderCache:
    def __init__(self, log_level="INFO"):
        self.objects = {}
        self.set_log_level(log_level)

    def set_log_level(self, level):
        logging.getLogger().setLevel(level)

    def get(self, key):
        return self.objects.get(key)

    def reset_cache(self):
        self.objects = {}

    def tessellate(
        self,
        compound,
        quality=0.1,
        angular_tolerance=0.1,
    ):

        hash = compound.HashCode(HASH_CODE_MAX)
        if self.objects.get(hash) is None:
            logging.debug(" Tessellate object(hash=%d)", hash)
            np_vertices, np_triangles, np_normals = tessellate(compound, quality, angular_tolerance)

            if np_normals.shape != np_vertices.shape:
                raise AssertionError("Wrong number of normals/shapes")

            shape_geometry = BufferGeometry(
                attributes={
                    "position": BufferAttribute(np_vertices),
                    "index": BufferAttribute(np_triangles.ravel()),
                    "normal": BufferAttribute(np_normals),
                }
            )

            self.objects[hash] = shape_geometry
        else:
            logging.debug(" Get object(hash=%d) from cache", hash)

        return self.objects[hash]

    def parallel_tessellate(self, shapes, quality=0.1, angular_tolerance=0.1):
        files = []
        for shape in shapes:
            with NamedTemporaryFile(delete=False) as ntf:
                serialize(shape, ntf.name)
                files.append((ntf.name, shape.HashCode(HASH_CODE_MAX)))

        with Pool() as p:
            result = p.map(_tessellate_wrapper(quality, angular_tolerance), files)
            for r in result:
                if r is not None:
                    np_vertices, np_triangles, np_normals = r[1]
                    self.objects[r[0]] = BufferGeometry(
                        attributes={
                            "position": BufferAttribute(np_vertices),
                            "index": BufferAttribute(np_triangles.ravel()),
                            "normal": BufferAttribute(np_normals),
                        }
                    )


RENDER_CACHE = RenderCache("DEBUG")
reset_cache = RENDER_CACHE.reset_cache


def material(color, transparent=False, opacity=1.0):
    material = CustomMaterial("standard")
    material.color = color
    material.clipping = True
    material.side = "DoubleSide"
    material.alpha = 0.7
    material.polygonOffset = True
    material.polygonOffsetFactor = 1
    material.polygonOffsetUnits = 1
    material.transparent = transparent
    material.opacity = opacity
    material.update("metalness", 0.3)
    material.update("roughness", 0.8)
    return material


class IndexedGroup(Group):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ind = None

    def dump(self, ind=""):
        print(ind, self.name, self.ind)
        for c in self.children:
            if isinstance(c, IndexedGroup):
                c.dump(ind + "    ")
            else:
                print(ind + "  ", c)

    def find_group(self, query):
        return tree_find_single_selector(self, query)


class IndexedMesh(Mesh):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ind = None

    def __repr__(self):
        return (
            f"IndexedMesh(name='{self.name}', ind={self.ind}, position={self.position}, quaternion={self.quaternion})"
        )


class IndexedPoints(Points):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ind = None

    def __repr__(self):
        return f"IndexedPoints(name='{self.name}', ind={self.ind}, position={self.position}, quaternion={self.quaternion})"


class IndexedLineSegments2(LineSegments2):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ind = None

    def __repr__(self):
        return f"IndexedLineSegments2(name='{self.name}', ind={self.ind}, position={self.position}, quaternion={self.quaternion})"


class CadqueryRenderer(object):
    def __init__(
        self,
        quality=0.1,
        angular_tolerance=0.1,
        render_edges=True,
        render_shapes=True,
        edge_accuracy=0.01,
        default_mesh_color=None,
        default_edge_color=None,
        parallel=False,
        timeit=False,
    ):
        self.quality = quality
        self.angular_tolerance = angular_tolerance
        self.edge_accuracy = edge_accuracy
        self.render_edges = render_edges
        self.render_shapes = render_shapes

        self.default_mesh_color = Color(default_mesh_color or (166, 166, 166))
        self.default_edge_color = Color(default_edge_color or (128, 128, 128))

        self.parallel = parallel
        self.timeit = timeit

    def _start_timer(self):
        if self.timeit:
            return time.time()

    def _stop_timer(self, msg, start):
        if self.timeit:
            print("%20s: %d ms" % (msg, int((time.time() - start) * 1000)))

    def render_shape(
        self,
        name,
        shape=None,
        edges=None,
        vertices=None,
        mesh_color=None,
        edge_color=None,
        vertex_color=None,
        render_edges=True,
        render_shapes=True,
        edge_width=1,
        vertex_width=5,
        transparent=False,
        opacity=1.0,
    ):

        edge_list = None
        edge_lines = None
        points = None
        shape_mesh = None

        start_render_time = self._start_timer()
        if shape is not None:
            if mesh_color is None:
                mesh_color = self.default_mesh_color
            if edge_color is None:
                edge_color = self.default_edge_color
            if vertex_color is None:
                vertex_color = self.default_edge_color  # same as edge_color

            # Compute the tesselation and build mesh
            start_tesselation_time = self._start_timer()

            shape_geometry = RENDER_CACHE.get(shape.HashCode(HASH_CODE_MAX))
            if shape_geometry is None:
                shape_geometry = RENDER_CACHE.tessellate(
                    shape,
                    self.quality,
                    self.angular_tolerance,
                )

            shp_material = material(mesh_color.web_color, transparent=transparent, opacity=opacity)
            # Do not cache building the mesh. Might lead to unpredictable results
            shape_mesh = IndexedMesh(geometry=shape_geometry, material=shp_material)

            self._stop_timer(" - build mesh time", start_tesselation_time)

            if render_edges:
                edges = get_edges(shape)

            # unset shape_mesh again
            if not render_shapes:
                shape_mesh = None

        if vertices is not None:
            vertices_list = []
            for vertex in vertices:
                vertices_list.append(get_point(vertex))
            vertices_list = np.array(vertices_list, dtype=np.float32)

            attributes = {"position": BufferAttribute(vertices_list, normalized=False)}

            mat = PointsMaterial(color=vertex_color.web_color, sizeAttenuation=False, size=vertex_width)
            geom = BufferGeometry(attributes=attributes)
            points = IndexedPoints(geometry=geom, material=mat)

        if edges is not None:
            start_discretize_time = self._start_timer()
            edge_list = [discretize_edge(edge, self.edge_accuracy) for edge in edges]
            self._stop_timer(" - discretize time", start_discretize_time)

        if edge_list is not None:
            start_discretize_time = self._start_timer()
            edge_list = flatten(list(map(explode, edge_list)))
            if isinstance(edge_color, (list, tuple)):
                if len(edge_list) != len(edge_color):
                    print("warning: color list and edge list have different length, using first color for all edges")
                    edge_color = edge_color[0]

            if isinstance(edge_color, (list, tuple)):
                lines = LineSegmentsGeometry(
                    positions=edge_list,
                    colors=[[color.percentage] * 2 for color in edge_color],
                )
                mat = LineMaterial(linewidth=edge_width, vertexColors="VertexColors")
            else:
                lines = LineSegmentsGeometry(positions=edge_list)
                mat = LineMaterial(linewidth=edge_width, color=edge_color.web_color)

            edge_lines = [IndexedLineSegments2(lines, mat)]
            self._stop_timer(" - edge list", start_discretize_time)

        self._stop_timer(f"render shape {name:30} time", start_render_time)

        return shape_mesh, edge_lines, points

    def _render(self, shapes, current, prefix=""):

        group = IndexedGroup()
        # we need to ensure unique names to enable Threejs animation later which currently doesn't
        # support directory names
        group.name = shapes["name"] if prefix == "" else f"{prefix}>{shapes['name']}"
        group.ind = current

        if shapes["loc"] is not None:
            group.position, group.quaternion = tq(shapes["loc"])

        for shape in shapes["parts"]:
            if shape.get("parts") is None:
                self._mapping[shape["ind"]] = {"mesh": None, "edges": None}

                # Assume that all are edges when first element is an edge
                if is_edge(shape["shape"][0]):
                    options = dict(edges=shape["shape"], edge_color=shape["color"], edge_width=3, render_edges=True)
                elif is_vertex(shape["shape"][0]):
                    options = dict(
                        vertices=shape["shape"], vertex_color=shape["color"], vertex_width=6, render_edges=False
                    )
                else:
                    # shape has only 1 object
                    options = dict(
                        shape=shape["shape"][0],
                        mesh_color=shape["color"],
                        render_shapes=self.render_shapes,
                        render_edges=self.render_edges,
                    )

                shape_mesh, edge_lines, points = self.render_shape(shapes["name"], **options)

                ind = len(group.children)
                if shape_mesh is not None:
                    shape_mesh.name = shape["name"]
                    shape_mesh.ind = {"group": (*current, ind), "shape": shape["ind"]}
                    group.add(shape_mesh)
                    self._mapping[shape["ind"]]["mesh"] = (*current, ind)
                    ind += 1

                if edge_lines is not None:
                    edge_group = IndexedGroup()
                    edge_group.name = "edges"
                    edge_group.ind = (*current, ind)
                    for j, edge in enumerate(edge_lines):
                        edge.name = shape["name"]
                        edge.ind = {"group": (*current, ind, j), "shape": shape["ind"]}
                        edge_group.add(edge)
                    group.add(edge_group)
                    self._mapping[shape["ind"]]["edges"] = (*current, ind)
                    ind += 1

                if points is not None:
                    points.name = shape["name"]
                    points.ind = {"group": (*current, ind), "shape": shape["ind"]}
                    group.add(points)
                    self._mapping[shape["ind"]]["mesh"] = (*current, ind)
                    ind += 1

            else:
                ind = len(group.children)
                group.add(self._render(shape, (*current, ind), group.name))

        return group

    def render(self, shapes):
        hashes = []

        def collect(shapes):
            all_shapes = []
            for shape in shapes:
                if shape.get("parts") is None:
                    if not is_edge(shape["shape"][0]) and not is_vertex(shape["shape"][0]):
                        hash = shape["shape"][0].HashCode(HASH_CODE_MAX)
                        if not hash in hashes and RENDER_CACHE.get(hash) is None:
                            all_shapes.append(shape["shape"][0])
                            hashes.append(hash)
                else:
                    all_shapes = all_shapes + collect(shape["parts"])
            return all_shapes

        start_render_time = self._start_timer()
        self._mapping = {}

        # Render all shapes via multiprocessing
        if self.parallel:
            print("Using multiprocessing")
            start_parallel_tessellate_time = self._start_timer()
            all_shapes = collect(shapes["parts"])
            if all_shapes:
                RENDER_CACHE.parallel_tessellate(all_shapes)
            self._stop_timer("parallel tessellation time", start_parallel_tessellate_time)

        rendered_objects = self._render(shapes, (), "")
        self._stop_timer("overall render time", start_render_time)
        return rendered_objects, self._mapping
