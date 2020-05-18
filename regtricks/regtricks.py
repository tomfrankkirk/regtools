import os.path as op 
import glob 
import os 
from textwrap import dedent
import tempfile
import subprocess
import copy

import nibabel
from nibabel import Nifti2Image, MGHImage
import numpy as np 
from fsl.data.image import Image as FSLImage
from fsl.wrappers import applywarp

from .image_space import ImageSpace
from . import x5_interface as x5 
from . import application_helpers as apply
from .fnirt_coefficients import FNIRTCoefficients, NonLinearProduct
from . import multiplication as multiply 


class Transform(object):
    """
    Base object for all transformations. This should never actually be 
    instantiated but is instead used to provide common functions
    """
    
    def __init__(self):
        raise NotImplementedError() 

    @property
    def src_header(self):
        """Nibabel header for the original source image, if present"""

        if self.src_spc is not None: 
            return self.src_spc.header 
        else: 
            return None 

    @property
    def ref_header(self):
        """Nibabel header for the original header image, if present"""

        if self.ref_spc is not None: 
            return self.ref_spc.header 
        else: 
            return None 

    def save(self, path):
        """Save transformation at path in X5 format (experimental)"""

        x5.save_manager(self, path)

    def inverse(self):
        """NB NonLinear classes explicitly override this"""

        constructor = type(self)
        return constructor(self.ref2src_world, src=self.ref_spc, 
                           ref=self.src_spc, convention='world')

    def __repr__(self):
        raise NotImplementedError()

    # We need to explicitly not implement np.array_ufunc to allow overriding
    # of __matmul__, see: https://github.com/numpy/numpy/issues/9028
    __array_ufunc__ = None 

    def __matmul__(self, other):

        other = cast_potential_array(other)
        high_type = multiply.get_highest_type(self, other)

        if high_type is Registration: 
            return multiply.registration(self, other)
        elif high_type is MotionCorrection: 
            return multiply.moco(self, other)
        elif high_type is NonLinearRegistration: 
            return multiply.nonlinearreg(self, other)
        elif high_type is NonLinearMotionCorrection:
            return multiply.nonlinearmoco(self, other)
        else: 
            raise NotImplementedError("Not Transformation objects")

    def __rmatmul__(self, other):

        other = cast_potential_array(other)
        high_type = multiply.get_highest_type(self, other)

        if high_type is Registration: 
            return multiply.registration(other, self)
        elif high_type is MotionCorrection: 
            return multiply.moco(other, self)
        elif high_type is NonLinearRegistration: 
            return multiply.nonlinearreg(other, self)
        elif high_type is NonLinearMotionCorrection:
            return multiply.nonlinearmoco(other, self)
        else: 
            raise NotImplementedError("Not Transformation objects")

    def apply_to_image(self, src, ref, cores=1, **kwargs):
        """
        Applies transformation to data array. If a registration is applied 
        to 4D data, the same transformation will be applied to all volumes 
        in the series. 

        Args:   
            src (str/NII/MGZ/FSLImage): image to transform 
            ref (str/NII/MGZ/FSLImage/ImageSpace): target space for data 
            cores (int): CPU cores to use for 4D data (not for applywarp)
            **kwargs: passed on to scipy.ndimage.map_coordinates

        Returns: 
            (np.array) transformed image data in ref voxel grid.
        """

        data, creator = apply.src_load_helper(src)
        resamp = self.apply_to_array(data, src, ref, cores, **kwargs)
        if not isinstance(ref, ImageSpace):
            ref = ImageSpace(ref)
        
        if creator is MGHImage:
            ret = MGHImage(resamp, ref.vox2world, ref.header)
            return ret 
        else: 
            ret = Nifti2Image(resamp, ref.vox2world, ref.header)
            if creator is FSLImage:
                return FSLImage(ret)
            else: 
                return ret 

    def apply_to_array(self, data, src, ref, cores=1, **kwargs):
        """
        Applies transformation to data array. If a registration is applied 
        to 4D data, the same transformation will be applied to all volumes 
        in the series. 

        Args:   
            data (array): 3D or 4D array. 
            src (str/NII/MGZ/FSLImage/ImageSpace): current space of data 
            ref (str/NII/MGZ/FSLImage/ImageSpace): target space for data 
            cores (int): CPU cores to use for 4D data (not for applywarp)
            **kwargs: passed on to scipy.ndimage.map_coordinates

        Returns: 
            (np.array) transformed image data in ref voxel grid.
        """

        if not isinstance(src, ImageSpace):
            src = ImageSpace(src)
        if not isinstance(ref, ImageSpace):
            ref = ImageSpace(ref)

        if not (data.shape[:3] == src.size).all(): 
            raise RuntimeError("Data shape does not match source space")

        resamp = apply.despatch(data, self, src, ref, cores, **kwargs)
        return resamp      


class Registration(Transform):
    """
    Represents a transformation between the source image and reference.
    If src and ref are given, the transformation is assumed to be in 
    FLIRT/FSL convention, otherwise it is assumed to be in world convention.

    Args: 
        src2ref: either a 4x4 np.array representing affine transformation
            from source to reference, or a path to a text-like file 
        src: (optional) either the path to the source image, or an ImageSpace
            object initialised with the source 
        ref: (optional) either the path to the reference image, or an 
            ImageSpace object initialised with the referende 
        convention: (optional) either "world" (assumed if src/ref not given),
            or "fsl" (assumed if src/ref given)
    """

    def __init__(self, src2ref, src=None, ref=None, convention=""):

        if isinstance(src2ref, str): 
            src2ref = np.loadtxt(src2ref)

        if (src2ref.shape != (4,4) 
                or (np.abs(src2ref[3,:] - [0,0,0,1]) > 1e-9).any()):
            raise RuntimeError("src2ref must be a 4x4 affine matrix, where ",
                               "the last row is [0,0,0,1].")

        if (src is not None) and (ref is not None):  
            if not isinstance(src, ImageSpace):
                src = ImageSpace(src)
            self.src_spc = src 
            if not isinstance(ref, ImageSpace):
                ref = ImageSpace(ref)
            self.ref_spc = ref 

            if convention == "":
                print("Assuming FSL convention")
                convention = "fsl"

        else: 
            self.src_spc = None
            self.ref_spc = None 
            if convention == "":
                print("Assuming world convention")
                convention = "world"

        if convention.lower() == "fsl":
            src2ref_world = (self.ref_spc.FSL2world 
                             @ src2ref @ self.src_spc.world2FSL)

        elif convention.lower() == "world":
            src2ref_world = src2ref 

        else: 
            raise RuntimeError("Unrecognised convention")

        self.__src2ref_world = src2ref_world

    def __len__(self):
        return 1 

    def __repr__(self):
        s = self._repr_helper(self.src_spc)
        r = self._repr_helper(self.ref_spc)
        
        formatter = "{:8.3f}".format 
        with np.printoptions(precision=3, formatter={'all': formatter}):
            text = (f"""\
                Registration (linear) with properties:
                source:        {s}, 
                reference:     {r}, 
                src2ref_world: {self.src2ref_world[0,:]}
                               {self.src2ref_world[1,:]}
                               {self.src2ref_world[2,:]}
                               {self.src2ref_world[3,:]}""")
        return dedent(text)

    def _repr_helper(self, spc):
        if not spc: 
            return "(none defined)"
        elif spc.file_name: 
            return self.spc.file_name
        else:  
            return "ImageSpace object"

    
    @property
    def ref2src_world(self):
        return np.linalg.inv(self.__src2ref_world)

    @property
    def src2ref_world(self):
        return self.__src2ref_world

    @classmethod
    def identity(cls, src=None, ref=None):
        return Registration(np.eye(4), src, ref, convention="world")

    def to_fsl(self, src, ref):
        """
        Return transformation in FSL convention, for given src and ref, 
        as np.array. This will be 3D in the case of MotionCorrections
        """

        if not isinstance(src, ImageSpace):
            src = ImageSpace(src)
        if not isinstance(ref, ImageSpace):
            ref = ImageSpace(ref)

        return ref.world2FSL @ self.src2ref_world @ src.FSL2world

    def save_txt(self, path):
        """Save as textfile at path"""
        np.savetxt(path, self.src2ref_world)

    def apply_to_grid(self, src):
        """
        Apply registration to the voxel grid of an image, retaining original
        voxel data (no resampling). This is equivalent to shifting the image
        within world space but not altering the contents of the image itself.

        Args: 
            src: str, nibabel Nifti/MGH or FSL Image to apply transform
        
        Returns: 
            image object, of same type as source. 
        """

        data, create = apply.src_load_helper(src)
        src_spc = ImageSpace(src)
        new_spc = src_spc.transform(self.src2ref_world)
               
        if create is MGHImage:
            ret = MGHImage(data, new_spc.vox2world, new_spc.header)
            return ret 
        else: 
            ret = Nifti2Image(data, new_spc.vox2world, new_spc.header)
            if create is FSLImage:
                return FSLImage(ret)
            else: 
                return ret 

    def resolve(self, src, ref, length=1):
        """
        Generator returning coordinate arrays that map from reference 
        voxel coordinates into source voxel coordinates, including the 
        transform itself.

        Args: 
            src (ImageSpace): in which data currently exists and interpolation
                will be performed
            ref (ImageSpace): in which data needs to be expressed
            length (int): number of arrays to yield (identical copies)

        Yields: 
            (np.ndarray, n x 3) coordinates on which to interpolate 
        """

        ref2src_vox = (src.world2vox @ self.ref2src_world @ ref.vox2world)
        ijk = ref.ijk_grid('ij').reshape(-1, 3)
        ijk = apply.aff_trans(ref2src_vox, ijk).T
        for _ in range(length):
            yield ijk 


class MotionCorrection(Registration):
    """
    A sequence of Registration objects, one for each volume in a timeseries. 
    For within-series motion correction (not using an external reference), 
    src and ref will refer to the same target. If only the src is given, then
    ref is assumed to be the same as src (ie, within-series), with FSL 
    convention. 

    Args: 
        mats: a path to a directory containing transformation matrices, in
            name order (all files will be loaded), or a list of individual
            filenames, or a list of np.arrays 
        src: (optional) either the path to the source image, or an ImageSpace
            object representing the source 
        ref: (optional) either the path to the reference image, or an Image
            Space representing the source (NB this is usually the same as 
            the src image)
        convention: (optional) the convention used for each transformation
            (if src and ref are given, 'fsl' is assumed, otherwise 'world')
    """

    def __init__(self, mats, src=None, ref=None, convention=None):

        if isinstance(mats, str):
            mats = sorted(glob.glob(op.join(mats, '*')))
            if not mats: 
                raise RuntimeError("Did not find any matrices in %s" % mats)

        if not convention: 
            if (src is not None):
                print("Assuming FSL convention")
                convention = "fsl"
            else: 
                print("Assuming world convention")
                convention = "world"
            
        self.__transforms = []
        for mat in mats:
            if isinstance(mat, (np.ndarray, str)): 
                m = Registration(mat, src, ref, convention)
            else: 
                m = mat 
            self.__transforms.append(m)

    def __len__(self):
        return len(self.transforms)

    def __repr__(self):
        t = self.transforms[0]
        s = self._repr_helper(self.src_spc)
        r = self._repr_helper(self.ref_spc)

        formatter = "{:8.3f}".format 
        with np.printoptions(precision=3, formatter={'all': formatter}):
            text = (f"""\
                MotionCorrection (linear) with properties:
                source:          {s}, 
                reference:       {r}, 
                series length:   {len(self)}
                src2ref_world_0: {t.src2ref_world[0,:]}
                                 {t.src2ref_world[1,:]}
                                 {t.src2ref_world[2,:]}
                                 {t.src2ref_world[3,:]}""")
        return dedent(text)

    @classmethod
    def identity(cls, length):
        return MotionCorrection([Registration.identity()] * length)

    @classmethod
    def from_registration(cls, reg, length):
        """
        Produce a MotionCorrection by repeating a Registration object 
        n times (eg, 10 copies of a single transform)
        """

        return MotionCorrection([reg.src2ref_world] * length,
                                 reg.src_spc, reg.ref_spc, "world")

    @property 
    def transforms(self):
        """List of Registration objects representing each volume of transform"""
        return self.__transforms

    @property 
    def src2ref_world(self):
        """List of src to ref transformation matrices"""
        return [ t.src2ref_world for t in self.transforms ]

    @property
    def ref2src_world(self):
        """List of ref to src transformation matrices"""
        return [ t.ref2src_world for t in self.transforms ]

    @property
    def src_spc(self):
        """ImageSpace for source of transform"""
        return self.transforms[0].src_spc 

    @property
    def ref_spc(self):
        """ImageSpace for reference of transform"""
        return self.transforms[0].ref_spc

    def to_fsl(self, src, ref):
        """Transformation matrices in FSL terms"""
        return [ t.to_fsl(src, ref) for t in self.transforms ]

    def save_txt(self, outdir, src=None, ref=None, convention="world", 
                 prefix="MAT_"):
        """
        Save individual transformation matrices in text format
        in outdir. Matrices will be named prefix_001... 

        Args: 
            outdir: directory in which to save 
            src: (optional) path to image, or ImageSpace, source space of
                transformation
            ref: as above, for reference space of transformation 
            convention: "world" or "fsl", if fsl then src/ref must be given
            prefix: prefix for naming each matrix
        """
        
        os.makedirs(outdir, exist_ok=True)
        for idx, r in enumerate(self.transforms):
            p = op.join(outdir, "MAT_{:04d}.txt".format(idx))
            r.save_txt(p, src, ref, convention)

    def resolve(self, src, ref, length=1):
        """
        Generator returning coordinate arrays that map from reference 
        voxel coordinates into source voxel coordinates, including the 
        transform itself.

        Args: 
            src (ImageSpace): in which data currently exists and interpolation
                will be performed
            ref (ImageSpace): in which data needs to be expressed
            length (int): number of arrays to yield (individual transforms 
                within the overall series, in order)

        Yields: 
            (np.ndarray, n x 3) coordinates on which to interpolate 
        """

        ijk = ref.ijk_grid('ij').reshape(-1, 3).T
        for _, r2s in zip(range(length), self.ref2src_world):
            ref2src_vox = (src.world2vox @ r2s @ ref.vox2world)
            ijk = apply.aff_trans(ref2src_vox, ijk)
            yield ijk 

class NonLinearRegistration(Transform):
    """
    Non linear registration transformation. Currently only FSL FNIRT warps
    are supported. 
    
    Args: 
        warp: path to FNIRT warp coefficient field 
        src: source (path, ImageSpace) used for generating FNIRT coefficients
        ref: reference (path, ImageSpace) used for generating FNIRT coefficients 
        premat: affine registration to apply prior to warp (note, the --aff 
            used when running FNIRT does not need to be supplied)
        postmat: affine to apply after warp 
    """

    def __init__(self, warp, src, ref, premat=np.eye(4), postmat=np.eye(4)):

        if not isinstance(ref, ImageSpace):
            ref = ImageSpace(ref)
        self.ref_spc = ref 

        if not isinstance(src, ImageSpace):
            src = ImageSpace(src)
        self.src_spc = src 

        self.warp = FNIRTCoefficients(warp, src, ref)
        self.premat = Registration(np.eye(4), src, ref, "world")
        self.postmat = Registration(np.eye(4), src, ref, "world")

    def __len__(self):
        return 1

    @classmethod
    def _manual_construct(cls, warp, src, ref, premat, postmat):
        """Manual constructor, to be used from __matmul__ and __rmatmul__"""
        
        x = cls.__new__(cls)
        x.warp = warp
        x.src_spc = src 
        x.ref_spc = ref 
        # assert type(premat) is Registration
        # assert type(postmat) is Registration
        x.premat = premat 
        x.postmat = postmat 
        return x 

    def inverse(self):
        """Iverse warpfield, via FSL invwarp"""

        # TODO: lazy evaluation of this?

        with tempfile.TemporaryDirectory() as d:
            oldcoeffs = op.join(d, 'oldcoeffs.nii.gz')
            newcoeffs = op.join(d, 'newcoeffs.nii.gz')
            old_src = op.join(d, 'src.nii.gz')
            old_ref = op.join(d, 'ref.nii.gz')
            self.warp.src_spc.touch(old_src)
            self.warp.ref_spc.touch(old_ref)
            nibabel.save(self.warp.coefficients, oldcoeffs)
            cmd = 'invwarp -w {} -o {} -r {}'.format(oldcoeffs, 
                                                     newcoeffs, old_src)
            subprocess.run(cmd, shell=True)
            newcoeffs = nibabel.load(newcoeffs)
            newcoeffs.get_data()
            inv = NonLinearRegistration(newcoeffs, old_ref, old_src)
        return inv 

    def premat_to_fsl(self, src, ref): 
        """Return list of premats in FSL convention""" 

        if type(self.premat) is Registration: 
            return self.premat.to_fsl(src, ref)
        else: 
            assert type(self.premat) is list
            return [ t.to_fsl(src, ref) for t in self.premat ]

    def postmat_to_fsl(self, src, ref): 
        """Return list of postmats in FSL convention""" 

        if type(self.postmat) is Registration: 
            return self.postmat.to_fsl(src, ref)
        else: 
            assert type(self.postmat) is list
            return [ t.to_fsl(src, ref) for t in self.postmat ]

    def __repr__(self):
        text = (f"""\
        NonLinearRegistration with properties:
        """)
        return dedent(text)

    def resolve(self, src, ref, length=1):
        """
        Generator returning coordinate arrays that map from reference 
        voxel coordinates into source voxel coordinates, including the 
        transform itself.

        Args: 
            src (ImageSpace): in which data currently exists and interpolation
                will be performed
            ref (ImageSpace): in which data needs to be expressed
            length (int): number of arrays to yield (repeated copies)

        Yields: 
            (np.ndarray, n x 3) coordinates on which to interpolate 
        """

        warped_fsl = next(self.warp.get_displacements(ref, self.postmat, length))
        ref2src_vox = (src.world2vox 
                       @ self.premat.ref2src_world 
                       @ self.warp.src_spc.FSL2world)

        ijk = apply.aff_trans(ref2src_vox, warped_fsl).T
        for _ in range(length):
            yield ijk 

class NonLinearMotionCorrection(NonLinearRegistration):
    """
    Only to be created by multiplication of other classes. 

    Args: 
        warp: FNIRTCoefficients object 
        src: src of transform
        ref: ref of transform
        premat: list of Registration objects
        postmat: list of Registration objects
    """

    def __init__(self, warp, src, ref, premat, postmat):
        
        self.warp = warp

        if not isinstance(ref, ImageSpace):
            ref = ImageSpace(ref)
        self.ref_spc = ref 

        if not isinstance(src, ImageSpace):
            src = ImageSpace(src)
        self.src_spc = src 

        assert (isinstance(premat, (Registration, np.ndarray)) 
                or isinstance(postmat, (Registration, np.ndarray)))

        if len(premat) > len(postmat):
            assert len(postmat) == 1, 'Different length pre/postmats given'
            postmat = MotionCorrection.from_registration(postmat, len(premat))
        
        elif len(postmat) > len(premat): 
            assert len(premat) == 1, 'Different length pre/postmats given'
            premat = MotionCorrection.from_registration(premat, len(postmat))

        else:
            if not len(premat) == len(postmat): 
                raise ValueError('Different length pre/postmats')

        self.premat = premat 
        self.postmat = postmat 

    def __len__(self):
        return len(self.premat)

    def __repr__(self):
        text = f"""\
                NonLinearMotionCorrection with properties:
                source:          {self.src_spc}, 
                reference:       {self.ref_spc}, 
                series length:   {len(self)}
                """
        return dedent(text)

    def resolve(self, src, ref, length=1):
        """
        Generator returning coordinate arrays that map from reference 
        voxel coordinates into source voxel coordinates, including the 
        transform itself.

        Args: 
            src (ImageSpace): in which data currently exists and interpolation
                will be performed
            ref (ImageSpace): in which data needs to be expressed
            length (int): number of arrays to yield (individual transforms 
                within the overall series, in order)

        Yields: 
            (np.ndarray, n x 3) coordinates on which to interpolate 
        """

        for idx, (pre, warped_fsl) in enumerate(zip(
                self.premat.ref2src_world, 
                self.warp.get_displacements(ref, self.postmat, length))): 

            ref2src_vox = (src.world2vox @ pre @ self.warp.src_spc.FSL2world)
            ijk = apply.aff_trans(ref2src_vox, warped_fsl).T
            yield ijk 


def chain(*args):
    """ 
    Concatenate a series of registrations.

    Args: 
        *args: Registration objects, given in the order that they need to be 
            applied (eg, for A -> B -> C, give them in that order and they 
            will be multiplied as C @ B @ A)

    Returns: 
        Registration object, with the first registration's source 
        and the last's reference (if these are not None)
    """

    if (len(args) == 1):
        chained = args
    else: 

        # As we cannot multiply two NLMCs together, if we find two adjacent
        # NLRs in sequence then multiply them together first (in anticpation
        # that they may later be promoted to MCs by other items in the chain.
        # The below block picks out adjacent NLs, handles them, and inserts
        # the result back into the appropriate spot 
        args = list(args)
        while True: 
            did_update = False 
            for idx in range(len(args)-2):
                if ((type(args[idx]) is NonLinearRegistration) 
                    and (type(args[idx+1]) is NonLinearRegistration)):
                    combined = chain(args[idx], args[idx+1])
                    # combined = Registration.identity()
                    args = args[:idx] + [combined] + args[idx+2:] 
                    did_update = True 
                    break 
            if not did_update: 
                break 

        if not all([isinstance(r, Transform) for r in args ]):
            raise RuntimeError("Each item in sequence must be a",
                               " Registration, MotionCorrection or NonLinearRegistration")
                               
        # We do the first pair explicitly (in case there are only two)
        # and then we do all others via pre-multiplication 
        chained = args[1] @ args[0]
        for r in args[2:]:
            chained = r @ chained 

    return chained 


def cast_potential_array(arr):
    """Helper to convert 4x4 arrays to Registrations if not already"""

    if type(arr) is np.ndarray: 
        assert arr.shape == (4,4)
        arr = copy.deepcopy(arr)
        arr = Registration(arr)
    return arr